import time
import copy

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from IPython.display import clear_output
from scipy import stats
from torch.utils.data import DataLoader, Subset
from tqdm import tqdm


class ResNetGrad(torch.nn.Module):
    def __init__(self, model):
        super(ResNetGrad, self).__init__()
        self.features = model.model[:-5]
        self.classifier = model.model[-5:]
        self.gradients = None

    def activations_hook(self, grad):
        self.gradients = grad

    def forward(self, x):
        x = self.features(x)
        h = x.register_hook(self.activations_hook)
        x = self.classifier(x)
        return x

    def get_activations_gradient(self):
        return self.gradients

    def get_activations(self, x):
        return self.features(x)

class GuidedBackprop():
    def __init__(self, model):
        self.model = model

    def guided_backprop(self, input, label):

        def hookfunc(module, gradInput, gradOutput):
            return tuple([(None if g is None else g.clamp(min=0)) for g in gradInput])

        input.requires_grad = True
        h = [0] * len(list(self.model.features) + list(self.model.classifier))
        for i, module in enumerate(list(self.model.features) + list(self.model.classifier)):
            if type(module) == torch.nn.ReLU:
                h[i] = module.register_backward_hook(hookfunc)

        self.model.eval()
        output = self.model(input)
        self.model.zero_grad()
        output[0][label].backward()
        grad = input.grad.data
        grad /= grad.max()
        return np.clip(grad.cpu().numpy(),0,1)


def get_masks(mod, image, mask_type='grad_cam', cl=0):
    model = copy.deepcopy(mod)
    model.eval()
    masks = []
    image = torch.tensor(image).unsqueeze(1).to(device)
    logit = model(image)

    if mask_type == 'grad_cam':
        logit[:,cl].backward()
        activation = model.get_activations(image).detach()
        act_grad  = model.get_activations_gradient()
        pool_act_grad = torch.mean(act_grad, dim=[2,3,4], keepdim=True)
        activation = activation * pool_act_grad
        heatmap = torch.sum(activation, dim=1)
        heatmap = F.relu(heatmap)
        heatmap /= torch.max(heatmap)
        heatmap = F.interpolate(heatmap.unsqueeze(0),(153, 189, 163), mode='trilinear', align_corners=False)
        masks.append(heatmap.cpu().numpy())

    elif mask_type == 'guided_backprop':
        gp = GuidedBackprop(model)
        pred = logit.data.max(1)[1].item()
        img_grad = gp.guided_backprop(image, pred)
        masks.append(img_grad)

    return np.concatenate(masks,axis=0).squeeze(axis=1)


def run_one_epoch(model, loader, criterion, train, device, optimizer=None):
    model.to(device)
    model.train(train)

    losses = []
    probs = []
    targets = []

    for data, target in tqdm(loader):
        data = data.to(device, dtype=torch.float)
        target = target.long().to(device)
        outputs = model(data)
        loss = criterion(outputs, target)
        if train and optimizer is not None:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        losses.append(loss.data.cpu().numpy())
        probs.extend(F.softmax(outputs, dim=-1).cpu().data.numpy()[:, 1])
        targets.extend(list(target.cpu().data.numpy()))
        del data, target, outputs, loss

    return losses, probs, targets


def train(
    model, optimizer, train_dataloader, val_dataloader, device,
    metric, verbose=0, model_save_path=None,
        max_epoch=10, eps=3e-3, max_patience=10):

    criterion = nn.CrossEntropyLoss()

    patience = 0
    best_metric = 0

    epoch_train_loss, last_train_loss, epoch_train_metric, last_train_metric, = [], None, [], None
    epoch_val_loss, last_val_loss, epoch_val_metric, last_val_metric, = [], None, [], None

    for epoch in range(max_epoch):
        start_time = time.time()

        # 1. Train
        train_losses, train_probs, train_targets = run_one_epoch(model, train_dataloader, criterion, True, device, optimizer)

        # 2. Inference
        if val_dataloader is not None:
            with torch.no_grad():
                val_losses, val_probs, val_targets = run_one_epoch(model, val_dataloader, criterion, False, device)

        # 3. Metrics
        epoch_train_loss.append(np.mean(train_losses))
        epoch_train_metric.append(metric(train_targets, train_probs))
        if val_dataloader is not None:
            epoch_val_loss.append(np.mean(val_losses))
            epoch_val_metric.append(metric(val_targets, val_probs))

        # 4. Print metrics
        if verbose:
            clear_output(True)
            print("Epoch {} of {} took {:.3f}s".format(epoch + 1, max_epoch, time.time() - start_time))
            print("  training loss (in-iteration): \t{:.6f}".format(epoch_train_loss[-1]))
            if val_dataloader is not None:
                print("  validation loss: \t\t\t{:.6f}".format(epoch_val_loss[-1]))
            print("  training {}: \t\t\t{:.2f}".format(metric.__name__, epoch_train_metric[-1]))
            if val_dataloader is not None:
                print("  validation {}: \t\t\t{:.2f}".format(metric.__name__, epoch_val_metric[-1]))

        # 5. Plot metrics
        if verbose:
            fig, axes = plt.subplots(1, 2, figsize=(10, 5))
            plt.figure(figsize=(10, 5))
            axes[0].plot(epoch_train_loss, label='train')
            if val_dataloader is not None:
                axes[0].plot(epoch_val_loss, label='val')
            axes[0].set_xlabel('epoch')
            axes[0].set_ylabel('loss')
            axes[0].legend()
            axes[1].plot(epoch_train_metric, label='train')
            if val_dataloader is not None:
                axes[1].plot(epoch_val_metric, label='val')
            axes[1].set_ylim([0, 1.05])
            axes[1].set_xlabel('epoch')
            axes[1].set_ylabel(metric.__name__)
            axes[1].legend()
            plt.show()

        # 5. Early stopping, best metrics, save model
        if val_dataloader is not None and epoch_val_metric[-1] > best_metric:
            patience = 0
            best_metric = epoch_val_metric[-1]
            last_train_metric, last_val_metric = epoch_train_metric[-1], epoch_val_metric[-1]
            last_train_loss, last_val_loss = epoch_train_loss[-1], epoch_val_loss[-1]
            if model_save_path is not None:
                    torch.save(model.state_dict(), model_save_path)
        elif val_dataloader is None and epoch_train_metric[-1] >= best_metric:
            patience = 0
            best_metric = epoch_train_metric[-1]
            last_train_metric = epoch_train_metric[-1]
            last_train_loss = epoch_train_loss[-1]
            if model_save_path is not None:
                torch.save(model.state_dict(), model_save_path)
        else:
            patience += 1

        if patience >= max_patience:
            print("Early stopping! Patience is out.")
            break
        if epoch_train_loss[-1] < eps:
            print("Early stopping! Train loss < eps.")
            break

    return last_train_loss, last_train_metric, last_val_loss, last_val_metric


def stratified_batch_indices(indices, labels):
    dominating_label = stats.mode(labels)[0][0]
    idx0 = indices[labels == dominating_label]
    idx1 = indices[labels != dominating_label]
    step = np.ceil(len(idx0) / len(idx1)) + 1
    assert step >= 1.
    result = []
    j0 = 0
    j1 = 0
    for i in range(len(indices)):
        if (i % step == 0 or j0 == len(idx0)) and j1 < len(idx1):
            result.append(idx1[j1])
            j1 += 1
        else:
            result.append(idx0[j0])
            j0 += 1
    result = np.array(result)
    assert len(result) == len(indices)
    return result


def cross_val_score(
        create_model_opt, train_dataset, cv, device, metric, model_load_path=None,
        batch_size=10, val_dataset=None, transfer=False, finetune=False, inter_method=None, masks_path=None):
    assert not (transfer and finetune)
    assert (transfer == False) or (transfer == True and model_load_path is not None)

    use_rest = True
    if val_dataset is None:  # smri case or fmri case without rest
        val_dataset = train_dataset
        use_rest = False

    cv_splits = list(cv.split(X=np.arange(len(train_dataset)), y=train_dataset.labels))
    val_metrics = []

    for i in range(len(cv_splits)):
        train_idx, val_idx = cv_splits[i]

        # train data
        if model_load_path is None or transfer or finetune:
            train_idx = stratified_batch_indices(train_idx, train_dataset.labels[train_idx])
            train_loader = DataLoader(Subset(train_dataset, train_idx),
                                      shuffle=False,
                                      batch_size=batch_size,
                                      drop_last=False)

        # val data
        if use_rest:
            val_mask = (np.isin(val_dataset.pids, train_dataset.pids[train_idx]) == False)
            val_idx = np.arange(len(val_dataset))[val_mask]
            del val_mask

        val_dataset_cv = Subset(val_dataset, val_idx),
                                shuffle=False,
                                batch_size=batch_size,
                                drop_last=False
        val_loader = DataLoader(val_dataset_cv)

        if model_load_path is None or transfer or finetune:  # train + validation
            if model_load_path is None:  # train from scratch
                model, optimizer = create_model_opt()
            elif transfer:  # train only last layers
                model, optimizer = create_model_opt(model_load_path, transfer=True)
            elif finetune:  # train not from scratch
                model, optimizer = create_model_opt(model_load_path)
            eps = 1e-2 if use_rest else 3e-3
            _, _, _, last_val_metric = train(model, optimizer, train_loader, val_loader, device,
                                             metric=metric, verbose=1, eps=eps)
            val_metrics.append(last_val_metric)
            del train_loader

        else:  # no train, just validation
            model, optimizer = create_model_opt(model_load_path, transfer=False)
            criterion = nn.CrossEntropyLoss()
            with torch.no_grad():
                val_losses, val_probs, val_targets = run_one_epoch(model, val_loader, criterion, False, device)
            val_metric = metric(val_targets, val_probs)
            val_metrics.append(val_metric)

            if inter_method is not None:
                for img_number in range(len(val_dataset_cv)):
                    masks = get_masks(model.to(device), val_dataset_cv[img_number][0], inter_method)
                    nib.save(nib.Nifti1Image(masks[0], affine=np.diag(np.ones(4)), header=nib.Nifti1Header()), masks_path + '/' + str(i) + '_' + str(img_number) + '.nii')


        del val_loader, model, optimizer

    return val_metrics
