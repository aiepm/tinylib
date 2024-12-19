import albumentations as A
import os
import torch

from albumentations.pytorch import ToTensorV2
from pile.datasets.imagenet import ImageNet1KDataset
from torch import nn
from torch.utils.data import DataLoader
from tqdm import tqdm

from pile.models.mobilenet_v4 import CustomSmall
from pile.schedulers import WarmupCosineScheduler
from torch import optim, Tensor
from pile.util import get_current_lr

BATCH_SIZE = 256
NUM_WORKERS = 16
NUM_EPOCHS = 36
DEVICE_NAME = 'cuda:0'
METRICS_UPDATE_STEP = 1

class TModel(nn.Module):
  def __init__(self, backbone, dropout=0.2):
    super().__init__()
    self.backbone = backbone
    self.classifier_head = nn.Sequential(
      nn.Linear(1280, 1024),
      nn.BatchNorm1d(1024),
      nn.ReLU6(),
      nn.Dropout(dropout),
      nn.Linear(1024, 1024),
      nn.BatchNorm1d(1024),
      nn.ReLU6(),
      nn.Linear(1024, 1000)
    )

  def forward(self, x: Tensor) -> Tensor:
    x = self.backbone(x)
    x = x.view(x.shape[0], -1)
    x = self.classifier_head(x)
    return x

def get_imagenet_dataloaders(data_dir, batch_size=32, num_workers=4):
  # Define transforms
  imagenet_mean = [0.485, 0.456, 0.406]
  imagenet_std = [0.229, 0.224, 0.225]

  train_transforms = A.Compose([
    A.RandomResizedCrop(height=224, width=224, 
                        scale=(0.08, 1.0), 
                        ratio=(0.75, 1.3333), p=1.0),
    A.HorizontalFlip(p=0.5),
    A.ShiftScaleRotate(shift_limit=0.0, scale_limit=0.0, rotate_limit=15,
                       border_mode=0, value=[0,0,0], p=0.5),
    A.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4, hue=0.1, p=1.0),
    A.CoarseDropout(max_holes=1, max_height=24, max_width=24, fill_value=0, p=0.5),
    A.Normalize(mean=imagenet_mean, std=imagenet_std),
    ToTensorV2()
  ])

  val_transforms = A.Compose([
    A.Resize(256, 256),
    A.CenterCrop(224, 224),
    A.Normalize(mean=imagenet_mean, std=imagenet_std),
    ToTensorV2()
  ])

  # Create datasets
  train_dataset = ImageNet1KDataset(
    root_dir=os.path.join(data_dir, 'train'),
    transform=train_transforms
  )
  val_dataset = ImageNet1KDataset(
    root_dir=os.path.join(data_dir, 'val'),
    transform=val_transforms
  )

  # Create dataloaders
  train_loader = DataLoader(
    train_dataset,
    batch_size=batch_size,
    shuffle=True,
    num_workers=num_workers,
    pin_memory=True,
    prefetch_factor=4
  )
  val_loader = DataLoader(
    val_dataset,
    batch_size=batch_size,
    shuffle=False,
    num_workers=num_workers,
    pin_memory=True
  )

  return train_loader, val_loader

@torch.no_grad()
def validate(model, dataloader, device, criterion):
  """Evaluate the model on a given dataloader and return avg_loss, top1_acc, top5_acc."""
  model.eval()
  total_loss = 0.0
  total_samples = 0
  top1_correct = 0
  top5_correct = 0

  for images, labels in tqdm(dataloader, 'Validation'):
    images, labels = images.to(device, non_blocking=True), labels.to(device, non_blocking=True)
    with torch.amp.autocast(device.type, dtype=torch.float16):
      outputs = model(images)
      loss = criterion(outputs, labels)

    batch_size = labels.size(0)
    total_loss += loss.item() * batch_size
    total_samples += batch_size

    # Calculate top-1 and top-5 accuracies
    _, pred = outputs.topk(5, 1, True, True)
    pred = pred.t()
    correct = pred.eq(labels.view(1, -1).expand_as(pred))

    # top-1
    top1_correct += correct[:1].float().sum()
    # top-5
    top5_correct += correct[:5].float().sum()

  avg_loss = total_loss / total_samples
  top1_acc = (top1_correct / total_samples) * 100.0
  top5_acc = (top5_correct / total_samples) * 100.0
  return avg_loss, top1_acc.item(), top5_acc.item()

def main():
  # Check if GPU is available
  device = torch.device(DEVICE_NAME if torch.cuda.is_available() else 'cpu')
  print(device)

  train_loader, test_loader = get_imagenet_dataloaders(
    '/core/datasets/imagenet/target_dir/',
    batch_size=BATCH_SIZE,
    num_workers=NUM_WORKERS,
  )

  NUM_STEPS = len(train_loader) * NUM_EPOCHS
  WARMUP_STEPS = NUM_STEPS // 100

  model = TModel(CustomSmall(), 0.2).to(device)
  criterion = nn.CrossEntropyLoss()
  optimizer = optim.SGD(
    model.parameters(),
    lr=0.08,
    nesterov=True,
    momentum=0.9,
    weight_decay=1e-4
  )
  scheduler = WarmupCosineScheduler(optimizer, NUM_STEPS, WARMUP_STEPS)

  #optimizer = optim.AdamW(model.parameters(), lr=4 * 1e-3)
  #scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', factor=0.5, patience=len(train_loader))

  train_metrics = {'top1': [], 'top5': [], 'loss': []}
  test_metrics = {'top1': [], 'top5': [], 'loss': []}

  scaler = torch.amp.GradScaler(device.type)
  for epoch in range(NUM_EPOCHS):
    model.train()
    current_lr = get_current_lr(optimizer)
    running_loss = 0.0
    running_top1_correct = 0.0
    running_top5_correct = 0.0
    running_samples = 0

    for images, labels in tqdm(train_loader, 'Train'):
      images, labels = images.to(device, non_blocking=True), labels.to(device, non_blocking=True)

      with torch.amp.autocast(device.type, dtype=torch.float16):
        outputs = model(images)
        loss = criterion(outputs, labels)

      optimizer.zero_grad()
      scaler.scale(loss).backward()
      if epoch > WARMUP_STEPS:
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
      scaler.step(optimizer)
      scaler.update()

      running_loss += loss.item() * labels.size(0)
      running_samples += labels.size(0)

      # Compute training top1 and top5 on the fly
      _, pred = outputs.topk(5, 1, True, True)
      pred = pred.t()
      correct = pred.eq(labels.view(1, -1).expand_as(pred))
      running_top1_correct += correct[:1].float().sum().item()
      running_top5_correct += correct[:5].float().sum().item()

      scheduler.step()

    avg_train_loss = running_loss / running_samples
    train_top1 = (running_top1_correct / running_samples) * 100.0
    train_top5 = (running_top5_correct / running_samples) * 100.0

    # Evaluate on test set every METRICS_UPDATE_STEP
    if epoch % METRICS_UPDATE_STEP == 0:
      avg_test_loss, test_top1, test_top5 = validate(model, test_loader, device, criterion)

      # Record metrics
      train_metrics['loss'].append(avg_train_loss)
      train_metrics['top1'].append(train_top1)
      train_metrics['top5'].append(train_top5)

      test_metrics['loss'].append(avg_test_loss)
      test_metrics['top1'].append(test_top1)
      test_metrics['top5'].append(test_top5)
    else:
      # If not evaluating this epoch, reuse last metrics for printing
      avg_test_loss = test_metrics['loss'][-1] if len(test_metrics['loss']) > 0 else 0.0
      test_top1 = test_metrics['top1'][-1] if len(test_metrics['top1']) > 0 else 0.0
      test_top5 = test_metrics['top5'][-1] if len(test_metrics['top5']) > 0 else 0.0

      # Still record training metrics for consistency
      train_metrics['loss'].append(avg_train_loss)
      train_metrics['top1'].append(train_top1)
      train_metrics['top5'].append(train_top5)

    print(
      f"Epoch [{epoch+1}/{NUM_EPOCHS}], "
      f"LR: {current_lr:.6f}, "
      f"Train Loss: {avg_train_loss:.4f}, "
      f"Test Loss: {avg_test_loss:.4f}, "
      f"Train Top1: {train_top1:.2f}%, Train Top5: {train_top5:.2f}%, "
      f"Test Top1: {test_top1:.2f}%, Test Top5: {test_top5:.2f}%"
    )

  # Print best results
  best_train_top1 = max(train_metrics['top1']) if train_metrics['top1'] else 0.0
  best_test_top1 = max(test_metrics['top1']) if test_metrics['top1'] else 0.0
  print('Best train top1 accuracy: ', best_train_top1)
  print('Best test top1 accuracy: ', best_test_top1)

if __name__ == "__main__":
  main()
