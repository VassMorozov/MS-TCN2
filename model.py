#!/usr/bin/python2.7

import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import optim
import copy
import numpy as np
from loguru import logger


class MS_TCN2(nn.Module):
    def __init__(self, num_layers_PG, num_layers_R, num_R, num_f_maps, dim, num_classes):
        super(MS_TCN2, self).__init__()
        self.PG = Prediction_Generation(num_layers_PG, num_f_maps, dim, num_classes)
        self.Rs = nn.ModuleList([copy.deepcopy(Refinement(num_layers_R, num_f_maps, num_classes, num_classes)) for s in range(num_R)])

    def forward(self, x):
        out = self.PG(x)
        outputs = out.unsqueeze(0)
        for R in self.Rs:
            out = R(F.softmax(out, dim=1))
            outputs = torch.cat((outputs, out.unsqueeze(0)), dim=0)

        return outputs

class Prediction_Generation(nn.Module):
    def __init__(self, num_layers, num_f_maps, dim, num_classes):
        super(Prediction_Generation, self).__init__()

        self.num_layers = num_layers

        self.conv_1x1_in = nn.Conv1d(dim, num_f_maps, 1)

        self.conv_dilated_1 = nn.ModuleList((
            nn.Conv1d(num_f_maps, num_f_maps, 3, padding=2**(num_layers-1-i), dilation=2**(num_layers-1-i))
            for i in range(num_layers)
        ))

        self.conv_dilated_2 = nn.ModuleList((
            nn.Conv1d(num_f_maps, num_f_maps, 3, padding=2**i, dilation=2**i)
            for i in range(num_layers)
        ))

        self.conv_fusion = nn.ModuleList((
             nn.Conv1d(2*num_f_maps, num_f_maps, 1)
             for i in range(num_layers)

            ))


        self.dropout = nn.Dropout()
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)

    def forward(self, x):
        f = self.conv_1x1_in(x)

        for i in range(self.num_layers):
            f_in = f
            f = self.conv_fusion[i](torch.cat([self.conv_dilated_1[i](f), self.conv_dilated_2[i](f)], 1))
            f = F.relu(f)
            f = self.dropout(f)
            f = f + f_in

        out = self.conv_out(f)

        return out

class Refinement(nn.Module):
    def __init__(self, num_layers, num_f_maps, dim, num_classes):
        super(Refinement, self).__init__()
        self.conv_1x1 = nn.Conv1d(dim, num_f_maps, 1)
        self.layers = nn.ModuleList([copy.deepcopy(DilatedResidualLayer(2**i, num_f_maps, num_f_maps)) for i in range(num_layers)])
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)

    def forward(self, x):
        out = self.conv_1x1(x)
        for layer in self.layers:
            out = layer(out)
        out = self.conv_out(out)
        return out
    
class MS_TCN(nn.Module):
    def __init__(self, num_stages, num_layers, num_f_maps, dim, num_classes):
        super(MS_TCN, self).__init__()
        self.stage1 = SS_TCN(num_layers, num_f_maps, dim, num_classes)
        self.stages = nn.ModuleList([copy.deepcopy(SS_TCN(num_layers, num_f_maps, num_classes, num_classes)) for s in range(num_stages-1)])

    def forward(self, x, mask):
        out = self.stage1(x, mask)
        outputs = out.unsqueeze(0)
        for s in self.stages:
            out = s(F.softmax(out, dim=1) * mask[:, 0:1, :], mask)
            outputs = torch.cat((outputs, out.unsqueeze(0)), dim=0)
        return outputs


class SS_TCN(nn.Module):
    def __init__(self, num_layers, num_f_maps, dim, num_classes):
        super(SS_TCN, self).__init__()
        self.conv_1x1 = nn.Conv1d(dim, num_f_maps, 1)
        self.layers = nn.ModuleList([copy.deepcopy(DilatedResidualLayer(2 ** i, num_f_maps, num_f_maps)) for i in range(num_layers)])
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)

    def forward(self, x, mask):
        out = self.conv_1x1(x)
        for layer in self.layers:
            out = layer(out, mask)
        out = self.conv_out(out) * mask[:, 0:1, :]
        return out


class DilatedResidualLayer(nn.Module):
    def __init__(self, dilation, in_channels, out_channels):
        super(DilatedResidualLayer, self).__init__()
        self.conv_dilated = nn.Conv1d(in_channels, out_channels, 3, padding=dilation, dilation=dilation)
        self.conv_1x1 = nn.Conv1d(out_channels, out_channels, 1)
        self.dropout = nn.Dropout()

    def forward(self, x):
        out = F.relu(self.conv_dilated(x))
        out = self.conv_1x1(out)
        out = self.dropout(out)
        return x + out


class Trainer:
    def __init__(self, num_layers_PG, num_layers_R, num_R, num_f_maps, dim, num_classes, dataset, split, gt_path, actions_dict, sample_rate, loss_cont=0.15):
        self.model = MS_TCN2(num_layers_PG, num_layers_R, num_R, num_f_maps, dim, num_classes)
        self.ce = nn.CrossEntropyLoss(ignore_index=-100)
        self.mse = nn.MSELoss(reduction='none')
        self.num_classes = num_classes
        self.gt_path = gt_path
        self.actions_dict = actions_dict
        self.sample_rate = sample_rate
        self.train_loss = []
        self.train_acc = []
        self.loss_cont = loss_cont

        logger.add('logs/' + dataset + "_" + split + "_{time}.log")
        logger.add(sys.stdout, colorize=True, format="{message}")

    def train(self, save_dir, batch_gen, num_epochs, batch_size, learning_rate, device, info_every=1):
        self.model.train()
        self.model.to(device)
        optimizer = optim.Adam(self.model.parameters(), lr=learning_rate)
        for epoch in range(num_epochs):
            epoch_loss = 0
            correct = 0
            total = 0
            while batch_gen.has_next():
                batch_input, batch_target, mask = batch_gen.next_batch(batch_size)
                batch_input, batch_target, mask = batch_input.to(device), batch_target.to(device), mask.to(device)
                optimizer.zero_grad()
                predictions = self.model(batch_input)

                loss = 0
                for p in predictions:
                    loss += self.ce(p.transpose(2, 1).contiguous().view(-1, self.num_classes), batch_target.view(-1))
                    loss += self.loss_cont*torch.mean(torch.clamp(self.mse(F.log_softmax(p[:, :, 1:], dim=1), F.log_softmax(p.detach()[:, :, :-1], dim=1)), min=0, max=16)*mask[:, :, 1:])

                epoch_loss += loss.item()
                loss.backward()
                optimizer.step()

                _, predicted = torch.max(predictions[-1].data, 1)
                correct += ((predicted == batch_target).float()*mask[:, 0, :].squeeze(1)).sum().item()
                total += torch.sum(mask[:, 0, :]).item()

            batch_gen.reset()
            torch.save(self.model.state_dict(), save_dir + "/epoch-" + str(epoch + 1) + ".model")
            torch.save(optimizer.state_dict(), save_dir + "/epoch-" + str(epoch + 1) + ".opt")
            
            if epoch % info_every == 0:
                logger.info("[epoch %d]: epoch loss = %f,   acc = %f" % (epoch + 1, epoch_loss / len(batch_gen.list_of_examples),
                                                                   float(correct)/total))
                        
            self.train_loss.append(epoch_loss / len(batch_gen.list_of_examples))
            self.train_acc.append(float(correct)/total)

    def predict(self, model_dir, results_dir, features_path, vid_list_file, epoch, actions_dict, device):
        self.model.eval()
        with torch.no_grad():
            self.model.to(device)
            self.model.load_state_dict(torch.load(model_dir + "/epoch-" + str(epoch) + ".model"))
            file_ptr = open(vid_list_file, 'r')
            list_of_vids = file_ptr.read().split('\n')[:-1]
            file_ptr.close()
            
            epoch_loss = 0
            total = 0
            correct = 0
            for vid in list_of_vids:
                #print vid
                features = np.load(features_path + vid.split('.')[0] + '.npy')
                
                #adding this so that I can have loss for test data
                file_ptr = open(self.gt_path + vid, 'r')
                content = file_ptr.read().split('\n')[:-1]
                classes = np.zeros(min(np.shape(features)[1], len(content)))
                for i in range(len(classes)):
                    classes[i] = self.actions_dict[content[i]]
                   
                
                classes = classes[::self.sample_rate]
                features = features[:, ::self.sample_rate]
                
                input_x = torch.tensor(features, dtype=torch.float)
                input_y = torch.tensor(classes,dtype=torch.long)
                
                input_x.unsqueeze_(0)
                input_x = input_x.to(device)
                
                input_y.unsqueeze_(0)
                input_y = input_y.to(device)
                
                mask = torch.ones(input_x.size(), device=device)
                
                predictions = self.model(input_x)
                loss = 0
                for p in predictions:
                    loss += self.ce(p.transpose(2, 1).contiguous().view(-1, self.num_classes), input_y.view(-1))
                    loss += self.loss_cont*torch.mean(torch.clamp(self.mse(F.log_softmax(p[:, :, 1:], dim=1), F.log_softmax(p.detach()[:, :, :-1], dim=1)), min=0, max=16))
                            
                epoch_loss += loss
                _, predicted = torch.max(predictions[-1].data, 1)
                
                correct += (predicted == input_y).sum()
                
                total += input_y.size()[1]
                
                predicted = predicted.squeeze()
                recognition = []
                for i in range(len(predicted)):
                    recognition = np.concatenate((recognition, [list(actions_dict.keys())[list(actions_dict.values()).index(predicted[i].item())]]*self.sample_rate))
                f_name = vid.split('/')[-1].split('.')[0]
                f_ptr = open(results_dir + "/" + f_name, "w")
                f_ptr.write("### Frame level recognition: ###\n")
                f_ptr.write(' '.join(recognition))
                f_ptr.close()
                
            return epoch_loss / len(list_of_vids), correct / total

