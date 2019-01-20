import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import uniform, normal

import os

from models import Generator, Discriminator, SoftmaxClassifier, Resnet101

class Trainer:
    def __init__(self, device, x_dim, z_dim, attr_dim, train_out, total_out, n_critic, lmbda, beta, bs):
        self.device = device

        self.x_dim = x_dim
        self.z_dim = z_dim
        self.attr_dim = attr_dim

        self.n_critic = n_critic
        self.lmbda = lmbda
        self.beta = beta
        self.bs = bs

        self.feature_extractor = Resnet101(finetune=False).to(self.device)

        self.eps_dist = uniform.Uniform(0, 1)
        self.Z_dist = normal.Normal(0, 1)

        self.eps_shape = torch.Size([bs, 1])
        self.z_shape = torch.Size([bs, z_dim])

        self.net_G = Generator(z_dim, attr_dim).to(self.device)
        self.optim_G = optim.Adam(self.net_G.parameters())

        self.net_D = Discriminator(x_dim, attr_dim).to(self.device)
        self.optim_D = optim.Adam(self.net_D.parameters())

        # classifier for judging the output of generator
        self.classifier = SoftmaxClassifier(x_dim, attr_dim, train_out).to(self.device)
        self.optim_cls = optim.Adam(self.classifier.parameters())

        # Final classifier trained on augmented data for GZSL
        self.final_classifier = SoftmaxClassifier(x_dim, attr_dim, total_out).to(self.device)
        self.optim_final_cls = optim.Adam(self.final_classifier.parameters())

        self.criterion_cls = nn.CrossEntropyLoss()

        self.model_save_dir = "saved_models"
        if not os.path.exists(self.model_save_dir):
            os.mkdir(self.model_save_dir)

    def get_conditional_input(self, X, C_Y):
        new_X = torch.cat([X, C_Y.float()], dim=1)
        return autograd.Variable(new_X).to(self.device)

    def fit_classifier(self, img, label_attr, label_idx):
        '''
        Train the classifier in supervised manner on a single
        minibatch of available data
        Args:
            img         -> bs X 3 X 224 X 224
            label_attr  -> bs X 102
            label_idx   -> bs
        Returns:
            loss for the minibatch
        '''
        label_idx = label_idx.to(self.device)
        label_attr = label_attr.to(self.device)

        img = autograd.Variable(img.squeeze()).to(self.device)
        img_features = self.feature_extractor(img)

        X_inp = self.get_conditional_input(img_features, label_attr)
        Y_pred = self.classifier(X_inp)

        self.optim_cls.zero_grad()
        loss = self.criterion_cls(Y_pred, label_idx)
        loss.backward()
        self.optim_cls.step()

        return loss.item()

    def get_gradient_penalty(self, X_real, X_gen):
        eps = self.eps_dist.sample(self.eps_shape).to(self.device)
        X_penalty = eps * X_real + (1 - eps) * X_gen

        X_penalty = autograd.Variable(X_penalty, requires_grad=True).to(self.device)
        critic_pred = self.net_D(X_penalty)
        grad_outputs = torch.ones(critic_pred.size()).to(self.device)
        gradients = autograd.grad(
                outputs=critic_pred, inputs=X_penalty,
                grad_outputs=grad_outputs,
                create_graph=True, retain_graph=True, only_inputs=True
        )[0]

        grad_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean()
        return grad_penalty

    def fit_GAN(self, img, label_attr, label_idx, use_cls_loss=True):
        L_gen = 0
        L_disc = 0
        total_L_disc = 0

        label_attr = label_attr.to(self.device)
        label_idx = label_idx.to(self.device)

        img = autograd.Variable(img.squeeze()).to(self.device)
        img_features = self.feature_extractor(img)

        # =============================================================
        # optimize discriminator
        # =============================================================
        X_real = self.get_conditional_input(img_features, label_attr)
        for _ in range(self.n_critic):
            Z = self.Z_dist.sample(self.z_shape).to(self.device)
            Z = self.get_conditional_input(Z, label_attr)

            X_gen = self.net_G(Z)
            X_gen = self.get_conditional_input(X_gen, label_attr)

            # calculate normal GAN loss
            L_disc = (self.net_D(X_gen) - self.net_D(X_real)).mean()

            # calculate gradient penalty
            grad_penalty = self.get_gradient_penalty(X_real, X_gen)
            L_disc += self.lmbda * grad_penalty

            # update critic params
            self.optim_D.zero_grad()
            L_disc.backward()
            self.optim_D.step()

            total_L_disc += L_disc.item()

        # =============================================================
        # optimize generator
        # =============================================================
        Z = self.Z_dist.sample(self.z_shape).to(self.device)
        Z = self.get_conditional_input(Z, label_attr)

        X_gen = self.net_G(Z)
        X = torch.cat([X_gen, label_attr.float()], dim=1)
        L_gen = -1 * torch.mean(self.net_D(X))

        if use_cls_loss:
            Y_pred = F.softmax(self.classifier(X), dim=0)
            log_prob = torch.log(torch.gather(Y_pred, 1, label_idx.unsqueeze(1)))
            L_cls = -1 * torch.mean(log_prob)
            L_gen += self.beta * L_cls

        self.optim_G.zero_grad()
        L_gen.backward()
        self.optim_G.step()

        return total_L_disc, L_gen.item()

    def fit_final_classifier(self, img, label_attr, label_idx, feature_input=False):
        label_attr = label_attr.to(self.device)
        label_idx = label_idx.to(self.device)

        if feature_input:
            img_features = img
        else:
            img = autograd.Variable(img.squeeze()).to(self.device)
            img_features = self.feature_extractor(img)

        X_inp = self.get_conditional_input(img_features, label_attr)
        Y_pred = self.final_classifier(X_inp)

        self.optim_final_cls.zero_grad()
        loss = self.criterion_cls(Y_pred, label_idx)
        loss.backward()
        self.optim_final_cls.step()

        return loss.item()

    def create_syn_dataset(self, labels):
        n_examples = 50
        syn_dataset = []
        for label_name, label_dict in labels.items():
            attr = label_dict['attribute']
            idx = label_dict['index']
            n_syn = n_examples if idx >= 645 else n_examples - 15

            z = self.Z_dist.sample(torch.Size([n_syn, self.z_dim]))
            c_y = torch.stack([torch.FloatTensor(attr) for _ in range(n_syn)])

            z_inp = self.get_conditional_input(z, c_y)
            X_gen = self.net_G(z_inp)

            syn_dataset.extend([(X_gen[i], attr, idx) for i in range(n_syn)])

        return syn_dataset

    def test(self, img, label_attr, label_idx):
        # eval mode
        self.final_classifier.eval()

        label_attr = label_attr.to(self.device)
        label_idx = label_idx

        X_inp = self.get_conditional_input(img, label_attr)
        with torch.no_grad():
            Y_pred = self.final_classifier(X_inp)
        _, Y_idx = torch.max(Y_pred, dim=1)

        correct = label_idx.eq(Y_idx.cpu()).sum()
        return correct / self.bs

    def save_model(self, model=None):
        if model == "disc_classifier":
            ckpt_path = os.path.join(self.model_save_dir, model + ".pth")
            torch.save(self.classifier.state_dict(), ckpt_path)

        elif model == "gan":
            g_ckpt_path = os.path.join(self.model_save_dir, "generator.pth")
            torch.save(self.net_G.state_dict(), g_ckpt_path)

            d_ckpt_path = os.path.join(self.model_save_dir, "discriminator.pth")
            torch.save(self.net_D.state_dict(), d_ckpt_path)

        elif model == "final_classifier":
            ckpt_path = os.path.join(self.model_save_dir, model + ".pth")
            torch.save(self.classifier.state_dict(), ckpt_path)

        else:
            raise Exception("Trying to save unknown model: %s" % model)

    def load_model(self, model=None):
        if model == "disc_classifier":
            ckpt_path = os.path.join(self.model_save_dir, model + ".pth")
            if os.path.exists(ckpt_path):
                self.classifier.load_state_dict(torch.load(ckpt_path))
                return True

        elif model == "gan":
            f1, f2 = False, False
            g_ckpt_path = os.path.join(self.model_save_dir, "generator.pth")
            if os.path.exists(g_ckpt_path):
                self.net_G.load_state_dict(torch.load(g_ckpt_path))
                f1 = True

            d_ckpt_path = os.path.join(self.model_save_dir, "discriminator.pth")
            if os.path.exists(d_ckpt_path):
                self.net_D.load_state_dict(torch.load(d_ckpt_path))
                f2 = True

            return f1 and f2

        elif model == "final_classifier":
            ckpt_path = os.path.join(self.model_save_dir, model + ".pth")
            if os.path.exists(ckpt_path):
                self.final_classifier.load_state_dict(torch.load(ckpt_path))
                return True

        else:
            raise Exception("Trying to load unknown model: %s" % model)

        return False