import statistics
import timeit
import os
import logging
import pdb
import numpy as np
import time

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader

from sklearn import metrics


class Trainer():
    def __init__(self, params, graph_classifier, train, onto, valid_evaluator=None,
                 onto_valid_evaluator=None):
        self.graph_classifier = graph_classifier
        self.valid_evaluator = valid_evaluator
        self.params = params
        self.train_data = train

        self.onto_valid_evaluator = onto_valid_evaluator
        self.onto_data = onto

        self.updates_counter = 0

        model_params = list(self.graph_classifier.parameters())
        logging.info('Total number of parameters: %d' % sum(map(lambda x: x.numel(), model_params)))

        if params.optimizer == "SGD":
            self.optimizer = optim.SGD(model_params, lr=params.lr, momentum=params.momentum,
                                       weight_decay=self.params.l2)
        if params.optimizer == "Adam":
            self.optimizer = optim.Adam(model_params, lr=params.lr, weight_decay=self.params.l2)

        self.criterion = nn.MarginRankingLoss(self.params.margin, reduction='sum')
        self.criterion2 = nn.MarginRankingLoss(self.params.margin2, reduction='sum')
        self.criterion3 = nn.MarginRankingLoss(self.params.margin3, reduction='sum')

        self.reset_training_state()

    def reset_training_state(self):
        self.best_metric = 0
        self.last_metric = 0
        self.not_improved_count = 0

    def train_epoch(self):
        total_loss = 0
        all_labels = []
        all_scores = []

        total_onto_loss = 0
        all_onto_labels = []
        all_onto_scores = []
        total_type_loss = 0
        all_type_labels = []
        all_type_scores = []

        dataloader = DataLoader(self.train_data, batch_size=self.params.batch_size, shuffle=True,
                                num_workers=self.params.num_workers, collate_fn=self.params.collate_fn)
        dataloader2 = DataLoader(self.onto_data, batch_size=self.params.batch_size, shuffle=True,
                                 num_workers=self.params.num_workers, collate_fn=self.params.collate_fn_onto)

        self.graph_classifier.train()
        model_params = list(self.graph_classifier.parameters())

        for b_idx, batch in enumerate(dataloader):
            data_pos, targets_pos, data_neg, targets_neg = self.params.move_batch_to_device(batch, self.params.device)
            self.optimizer.zero_grad()
            score_pos, score_type_pos, score_type_neg, score_idx = self.graph_classifier(data_pos, cal_type=True)
            score_neg = self.graph_classifier(data_neg)
            loss = self.criterion(score_pos.squeeze(1), score_neg.view(len(score_pos), -1).mean(dim=1),
                                  torch.Tensor(np.ones(len(score_pos))).to(device=self.params.device))

            loss_type = 0
            if len(score_idx) != 0:
                loss_type = self.criterion3(score_type_pos, score_type_neg.view(len(score_type_pos), -1).mean(dim=1),
                                            torch.Tensor(np.ones(len(score_pos))*-1).to(device=self.params.device))
                loss = loss + self.params.omega * loss_type

            loss.backward()
            self.optimizer.step()
            self.updates_counter += 1

            with torch.no_grad():
                all_scores += score_pos.squeeze().detach().cpu().tolist() + score_neg.squeeze().detach().cpu().tolist()
                all_labels += targets_pos.tolist() + targets_neg.tolist()
                total_loss += loss

                if len(score_idx) != 0:
                    score_type_pos_list = score_type_pos.detach().cpu().tolist()
                    score_type_neg_list = score_type_neg.detach().cpu().tolist()
                    add_pos_list = []
                    add_neg_list = []
                    for index in range(len(score_type_pos_list)):
                        if score_type_pos_list[index] < 1e-6:
                            continue
                        add_pos_list.append(score_type_pos_list[index])
                        add_neg_list.append(score_type_neg_list[index])
                    all_type_scores += add_pos_list + add_neg_list
                    all_type_labels += [0] * len(add_pos_list) + [1] * len(add_neg_list)
                total_type_loss += loss_type
                total_loss += loss_type

            if self.valid_evaluator and self.params.eval_every_iter and self.updates_counter % self.params.eval_every_iter == 0:
                tic = time.time()
                result = self.valid_evaluator.eval()
                logging.info('\nPerformance:' + str(result) + 'in ' + str(time.time() - tic))

                if result['auc'] >= self.best_metric:
                    self.save_classifier()
                    self.best_metric = result['auc']
                    self.not_improved_count = 0

                else:
                    self.not_improved_count += 1
                    if self.not_improved_count > self.params.early_stop:
                        logging.info(
                            f"Validation performance didn\'t improve for {self.params.early_stop} epochs. Training stops.")
                        break
                self.last_metric = result['auc']

        for b_idx, batch in enumerate(dataloader2):
            data_pos, targets_pos, data_neg, targets_neg = self.params.move_batch_to_device_onto(batch,
                                                                                                 self.params.device)
            self.optimizer.zero_grad()
            score_onto_pos = self.graph_classifier(data_pos, cal_onto=True)
            score_onto_neg = self.graph_classifier(data_neg, cal_onto=True)
            loss = self.params.alpha * self.criterion2(score_onto_pos,
                                                       score_onto_neg.view(len(score_onto_pos), -1).mean(dim=1),
                                                       torch.Tensor([-1]).to(device=self.params.device))
            loss.backward()
            self.optimizer.step()
            self.updates_counter += 1

            with torch.no_grad():
                all_onto_scores += score_onto_pos.squeeze().detach().cpu().tolist() + score_onto_neg.squeeze().detach().cpu().tolist()
                all_onto_labels += targets_pos.tolist() + targets_neg.tolist()
                total_onto_loss += loss
                total_loss += loss

        auc = metrics.roc_auc_score(all_labels, all_scores)
        auc_pr = metrics.average_precision_score(all_labels, all_scores)
        auc_type = metrics.roc_auc_score(all_type_labels, all_type_scores)
        auc_pr_type = metrics.average_precision_score(all_type_labels, all_type_scores)
        auc_onto = metrics.roc_auc_score(all_onto_labels, all_onto_scores)
        auc_pr_onto = metrics.average_precision_score(all_onto_labels, all_onto_scores)

        weight_norm = sum(map(lambda x: torch.norm(x), model_params))

        return total_loss, total_type_loss, total_onto_loss, auc, auc_pr, auc_type, auc_pr_type, auc_onto, auc_pr_onto, weight_norm

    def train(self):
        self.reset_training_state()

        for epoch in range(1, self.params.num_epochs + 1):
            time_start = time.time()
            loss, type_loss, onto_loss, auc, auc_pr, auc_type, auc_pr_type, auc_onto, auc_pr_onto, weight_norm = self.train_epoch()
            time_elapsed = time.time() - time_start
            logging.info(
                f'Epoch {epoch} with loss: {loss}, type loss: {type_loss}, onto loss: {onto_loss}, training auc: {auc}, training auc_pr: {auc_pr}, training type auc: {auc_type}, training type auc_pr: {auc_pr_type}, training onto auc: {auc_onto}, training onto auc_pr: {auc_pr_onto}, best validation AUC: {self.best_metric}, weight_norm: {weight_norm} in {time_elapsed}')

            if epoch % self.params.save_every == 0:
                torch.save(self.graph_classifier, os.path.join(self.params.exp_dir, 'graph_classifier_chk.pth'))

    def save_classifier(self):
        torch.save(self.graph_classifier, os.path.join(self.params.exp_dir,
                                                       'best_graph_classifier.pth'))  # Does it overwrite or fuck with the existing file?
        logging.info('Better models found w.r.t accuracy. Saved it!')
