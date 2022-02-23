import copy
import os
import random
import json
import time
import numpy as np
from tensorboardX import SummaryWriter
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from utils.utils import LinearSchedule, PiecewiseSchedule, ReplayBuffer, PrioritizedReplayBuffer
from utils.utils import get_sa_score, sa_gaussian_wrapper, sc_gaussian_wrapper, smi_wrapper
# from utils.utils import get_sa_score, get_sc_score, sa_gaussian_wrapper, sc_gaussian_wrapper, smi_wrapper
# from environments import synth_env as envs
from environments import environments as envs
from networks.nn_utils import initialize_weights
from networks import mpnn
from networks import mlp
from networks import noisy_net
from networks.distributional_net import DistributionalMultiLayerNetwork


class DQN(object):
    """The Deep Q Learning Agent class""" 
    def __init__(self,
                max_oracle_call, 
                q_fn, 
                score_fn,
                task='moldqn',
                # objective,
                args=None,
                param=None,
                keep=250,
                model_path='./checkpoints',
                gen_file='./mol_gen_'):
        """ 
        :param task: Task name, used to name the output files
        :param q_fn: The type of network used to learn the state value, see parsing.py
        :param objective: The generation environment, see parsing.py
        :param score_fn: The objective function, designed for Guacamol benchmarks
        :param args: Arguments, see parsing.py
        :param param: Path to parameter file
        :param keep: Number of molecules to keep in training process
        :param model_path: Path to store checkpoints.
        :param gen_file: The name of file to store generated molecules
        """
        self.max_oracle_call = max_oracle_call 
        self.score_fn = score_fn 
        self.task = task
        self.args = args
        self.num_episodes = self.args.num_episodes
        self.max_steps_per_episode = self.args.max_steps_per_episode
        self.batch_size = self.args.batch_size
        self.gamma = self.args.gamma
        # self.smiles_dict = dict() 

        # env_options = {
        #     'logp_scaled': envs.OptLogPMolecule,
        #     'logp': envs.OptRawLogPMolecule,
        #     'qed': envs.OptQEDMolecule, }

        # if objective is not None:
        #     assert objective in env_options.keys()
        #     env = env_options[objective]
        #     self.env = env(
        #         discount_factor=self.args.discount_factor,
        #         init_mol=self.args.init_mol,
        #         atom_types=set(self.args.atom_types),
        #         allow_removal=self.args.allow_removal,
        #         allow_no_modification=self.args.allow_no_modification,
        #         allow_bonds_between_rings=self.args.allow_bonds_between_rings,
        #         allowed_ring_sizes=set(self.args.allowed_ring_sizes),
        #         max_steps=self.args.max_steps_per_episode,
        #         args=self.args
        #     )
        # else:
        #     # Case to use Guacamol benchmark
        #     self.env = envs.OptGuacamol(
        #         scoring_function=score_fn,
        #         discount_factor=self.args.discount_factor,
        #         init_mol=self.args.init_mol,
        #         atom_types=set(self.args.atom_types),
        #         allow_removal=self.args.allow_removal,
        #         allow_no_modification=self.args.allow_no_modification,
        #         allow_bonds_between_rings=self.args.allow_bonds_between_rings,
        #         allowed_ring_sizes=set(self.args.allowed_ring_sizes),
        #         max_steps=self.args.max_steps_per_episode,
        #         args=self.args
        #     )

        """
                 atom_types,
                 discount_factor,
                 oracle, 
                 init_mol='C',
                 allow_removal=True,
                 allow_no_modification=True,
                 allow_bonds_between_rings=True,
                 allowed_ring_sizes=None,
                 max_steps=10,
                 target_fn=None,
                 record_path=False,
                 args=None
        """ 

        self.env = envs.Molecule(
                atom_types=set(self.args.atom_types), 
                discount_factor=self.args.discount_factor, 
                oracle = self.score_fn, 
                init_mol=self.args.init_mol,
                allow_removal=self.args.allow_removal,
                allow_no_modification=self.args.allow_no_modification,
                allow_bonds_between_rings=self.args.allow_bonds_between_rings,
                allowed_ring_sizes=set(self.args.allowed_ring_sizes),
                max_steps=self.args.max_steps_per_episode,
                args=self.args) 

        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        if torch.cuda.is_available():
            torch.cuda.set_device(0)
        print(f'Using Device: {self.device}')

        q_fn_options = {
            'mlp': mlp.MultiLayerNetwork if not args.noisy else noisy_net.MultiLayerNetwork,
            'mpnn': mpnn.MessagePassingNetwork
        }
        self.model_type = q_fn 
        if not self.args.distribution:
            model = q_fn_options[q_fn]
        else:
            model = DistributionalMultiLayerNetwork

        self.q_fn = model(self.args, self.device)
        initialize_weights(self.q_fn)
        if param is not None:
            self.q_fn.load_state_dict(torch.load(param))

        self.q_fn_target = copy.deepcopy(self.q_fn).eval()

        # For multiple GPUs run
        if torch.cuda.device_count() > 1:
            self.q_fn_target = MyDataParallel(self.q_fn_target)
            self.q_fn = MyDataParallel(self.q_fn)

        self.q_fn_target = self.q_fn_target.to(self.device)
        self.q_fn = self.q_fn.to(self.device)

        # Training attribute
        self.learning_frequency = self.args.learning_frequency
        self.learning_rate_decay_steps = self.args.learning_rate_decay_steps
        self.grad_clipping = self.args.grad_clipping

        self.optimizer = optim.Adam(
            params=self.q_fn.parameters(),
            lr=self.args.learning_rate,
            betas=(self.args.adam_beta_1, self.args.adam_beta_2),
            eps=1e-08,
            weight_decay=0,
            amsgrad=False
        )

        self.lr_schedule = optim.lr_scheduler.ExponentialLR(
            optimizer=self.optimizer,
            gamma=self.args.learning_rate_decay_rate
        )

        if not os.path.exists(model_path):
            os.makedirs(model_path)
        self.log_path = os.path.join(model_path, self.task)
        self.writer = SummaryWriter(self.log_path)

        # Logging attribute
        self.save_frequency = self.args.save_frequency
        self.tracker = Storage(keep=keep)

        # Generation options
        # self.gen_epsilon = self.args.gen_epsilon
        self.gen_file = gen_file + task + '.json'
        # self.gen_num_episode = self.args.gen_number

        # epsilon-greedy exploration schedule
        if not args.noisy:
            self.exploration = PiecewiseSchedule(
                [(0, 0), (int(self.num_episodes * 2 / 3), 0), (self.num_episodes, 0)],
                outside_value=0.01
            )
        else:
            self.exploration = PiecewiseSchedule(
                [(0, 0), (self.num_episodes, 0)],
                outside_value=0.01
            )

        # Replay buffer option
        self.prioritized = self.args.prioritized
        self.replay_buffer_size = self.args.replay_buffer_size
        self.prioritized_alpha = self.args.prioritized_alpha
        self.prioritized_beta = self.args.prioritized_beta
        self.prioritized_epsilon = self.args.prioritized_epsilon

        if self.prioritized:
            self.memory = PrioritizedReplayBuffer(self.replay_buffer_size, self.prioritized_alpha)
            self.beta_schedule = LinearSchedule(self.num_episodes, initial_p=self.prioritized_beta, final_p=0)
        else:
            self.memory = ReplayBuffer(self.replay_buffer_size)
            self.beta_schedule = None

        # Double DQN option
        self.double = self.args.double_q
        self.update_frequency = self.args.update_frequency

        # Bootstrap option
        self.num_bootstrap_heads = self.args.num_bootstrap_heads

        # Distributional option
        self.vmin=0
        self.vmax=1

    def train(self):
        if self.args.verbose:
            print(self.q_fn)

        global_step = 0
        for episode in range(self.num_episodes):
            global_step = self._episode(episode, global_step)


            oracle_num = self.env.called_oracle_number()
            if oracle_num > self.max_oracle_call:
                return self.env.smiles_dict 

            # Save checkpoint
            if episode % self.save_frequency == 0:
                model_name = 'dqn_checkpoint_' + str(episode) + '.pth'
                torch.save(self.q_fn.state_dict(), os.path.join(self.log_path, model_name))

        with open(self.gen_file, 'w') as f:
            json.dump(self.tracker.content, f)

        return self.env.smiles_dict 
        # return self.tracker.content

    def _episode(self,
                 episode,
                 global_step):
        """
        A function to run one generation episode.

        :param episode: The number of episode, used to adjust exploration rate.
        :param global_step: Total number of steps.
        :return: New global step.
        """

        episode_start_time = time.time()
        epsilon = self.exploration.value(episode)

        state_mol, state_step = self.env.reset()
        head = np.random.randint(self.num_bootstrap_heads)

        for step in range(self.max_steps_per_episode):

            state_mol, reward, done = self._step(epsilon=epsilon, head=head)

            # When finish an episode
            if done:

                print('Episode %d/%d took %gs' % (episode + 1, self.num_episodes, time.time() - episode_start_time))
                print('SMILES: %s' % state_mol)
                print('The reward is: %s' % str(reward))
                # print('The SA score is: %s' % str(sa_score))
                # print('The scaled reward is: %s\n' % str(reward_scaled))

                # Keep track the result
                # if reward > self.tracker.lowest:
                # self.tracker.insert((state_mol, reward_scaled, reward, sa_score))
                self.writer.add_text('SMILES reward', state_mol + ' Reward: ' + str(reward), episode)

                # Log result
                # self.writer.add_scalar('scaled_reward', reward_scaled, episode)
                self.writer.add_scalar('reward', reward, episode)
                # self.writer.add_scalar('sa', sa_score, episode)

            # Training the network
            start_train = 3 if self.args.noisy else 50
            if (episode > min(start_train, self.num_episodes / 10)) and (global_step % self.learning_frequency == 0):

                # Update learning rate
                if (global_step % self.learning_rate_decay_steps == 0) and (self.lr_schedule is not None):
                    self.lr_schedule.step()

                # Compute td error and optimize the network
                td_error = self._compute_td_loss(self.batch_size, episode)

                # Log result
                print('Current TD error: %.4f' % np.mean(np.abs(td_error)))
                self.writer.add_scalar('td_error', td_error, episode)

                # Update the target network
                if self.double and (episode % self.update_frequency == 0):
                    self.q_fn_target.load_state_dict(self.q_fn.state_dict())

            global_step += 1

        return global_step

    def _step(self,
              epsilon,
              head,
              gen=False):
        """
        A function to run one generation step

        :param epsilon: parameter for epsilon-greedy
        :param head:
        :param gen:
        :return:
        """

        # Get observation from current state
        observation = list(self.env.get_valid_actions())
        steps_left = self.max_steps_per_episode - self.env.num_steps_taken

        # Choose an action
        action = self.get_action(observation, epsilon, head)

        # Take a step forward
        next_state_mol, _, reward, done = self.env.step(action)

        # Synthesizability consideration
        # sa_score = get_sa_score(next_state_mol)

        # if self.args.synthesizability is None:
        #     reward_scaled = reward
        # elif self.args.synthesizability == 'sa':
        #     reward_scaled = reward * sa_gaussian_wrapper(sa_score)
        # elif self.args.synthesizability == 'sc':
        #     reward_scaled = reward * sc_gaussian_wrapper(get_sc_score(next_state_mol))
        # elif self.args.synthesizability == 'smi':
        #     reward_scaled = reward * smi_wrapper(len(next_state_mol))

        # Get new observation for updating the value network
        next_observations = list(self.env.get_valid_actions())
        steps_left_new = self.max_steps_per_episode - self.env.num_steps_taken

        if self.model_type == 'mlp':
            obs_t = self.q_fn.encoder(action, steps_left)
            obs_tp1 = torch.stack([self.q_fn.encoder(smile, steps_left_new) for smile in next_observations], dim=0)
        else:
            obs_t = (action, steps_left)
            obs_tp1 = (next_observations, steps_left_new)


        if not gen:
            self.memory.add(
                obs_t=obs_t,
                action=0,
                reward=reward,
                obs_tp1=obs_tp1,
                done=float(done)
            )

        return next_state_mol, reward, done

    def _compute_td_loss(self, batch_size, episode):
        """
        Compute the td error and update the network

        :param batch_size: Batch size.
        :param episode: Number of episodes.
        :return: TD error
        """
        if self.prioritized:
            obs, _, reward, next_obs, done, weight, indices = \
                self.memory.sample(batch_size, beta=self.beta_schedule.value(episode))
        else:
            obs, _, reward, next_obs, done = self.memory.sample(batch_size)
            weight = np.ones(np.array(reward).shape)
            indices = 0

        reward = torch.Tensor(np.array(reward)).to(self.device)
        done = torch.Tensor(np.array(done)).to(self.device)
        weight = torch.Tensor(np.array(weight)).to(self.device)
        # indices = torch.Tensor(np.array(indices)).to_(self.device)

        self.q_fn.train()
        self.q_fn_target.train()

        if self.args.distribution:

            # Case for distributional DQN
            support = torch.linspace(self.vmin, self.vmax, self.num_bootstrap_heads).to(self.device)
            delta_z = float(self.vmax - self.vmin) / (self.num_bootstrap_heads - 1)

            if self.model_type == 'mlp':
                dist_q_t = self.q_fn.forward(torch.stack(obs, dim=0).to(self.device)).view(-1, self.num_bootstrap_heads)
            else:
                dist_q_t = torch.stack(
                    [
                        self.q_fn.forward(s[0], [np.array(s[1])])
                        for s in obs
                    ], dim=0
                ).squeeze(1)

            if self.model_type == 'mlp':
                dist_q_tp1 = [self.q_fn_target.forward(s.to(self.device)).view(-1, self.num_bootstrap_heads) for s in
                         next_obs]
            else:
                dist_q_tp1 = [
                    self.q_fn_target.forward(s[0], [np.array(s[1])])
                    for s in next_obs
                ]

            q_t = dist_q_t * support
            q_tp1 = [q * support for q in dist_q_tp1]

            if self.double:

                if self.model_type == 'mlp':
                    dist_q_tp1_online = [self.q_fn.forward(s.to(self.device)).view(-1, self.num_bootstrap_heads)
                                         for s in next_obs]
                else:
                    dist_q_tp1_online = [
                        self.q_fn.forward(s[0], [np.array(s[1])])
                        for s in next_obs
                    ]

                q_tp1_online = [q * support for q in dist_q_tp1_online]
                q_tp1_online_idx = [q.sum(1).max(0)[1].data for q in q_tp1_online]
                v_tp1 = torch.stack([q[idx] for q, idx in zip(q_tp1, q_tp1_online_idx)], dim=0)
                dist_v_tp1 = torch.stack([q[idx] for q, idx in zip(dist_q_tp1, q_tp1_online_idx)], dim=0)

            else:
                q_tp1_idx = [q.sum(1).max(0)[1].data for q in q_tp1]
                v_tp1 = torch.stack([q[idx] for q, idx in zip(q_tp1, q_tp1_idx)], dim=0)
                dist_v_tp1 = torch.stack([q[idx] for q, idx in zip(dist_q_tp1, q_tp1_idx)], dim=0)

            rewards = reward.unsqueeze(1).expand_as(dist_v_tp1)
            dones = done.unsqueeze(1).expand_as(dist_v_tp1)
            support = support.unsqueeze(0).expand_as(dist_v_tp1)

            Tz = rewards + (1 - dones) * self.gamma * support
            Tz = Tz.clamp(min=self.vmin, max=self.vmax)
            b = (Tz - self.vmin) / delta_z
            l = b.floor().long().to(self.device)
            u = b.ceil().long().to(self.device)

            offset = torch.linspace(0, (batch_size - 1) * self.num_bootstrap_heads, batch_size).long() \
                .unsqueeze(1).expand(batch_size, self.num_bootstrap_heads).to(self.device)

            proj_dist = torch.zeros(dist_v_tp1.size()).to(self.device)
            proj_dist.view(-1).index_add_(0, (l + offset).view(-1), (dist_v_tp1 * (u.float() - b)).view(-1))
            proj_dist.view(-1).index_add_(0, (u + offset).view(-1), (dist_v_tp1 * (b - l.float())).view(-1))
            proj_dist.to(self.device)

            loss = - (proj_dist * dist_q_t.log()).sum(1)

            prios = loss.data.cpu().numpy() + self.prioritized_epsilon
            loss = loss.mul(weight).mean()

            q_tp1_masked = (1.0 - done.unsqueeze(1)) * v_tp1
            td_target = reward.unsqueeze(1) + self.gamma * q_tp1_masked
            td_error = (q_t - td_target).pow(2).mean()

        else:

            # Case for normal DQN
            if self.model_type == 'mlp':
                q_t = self.q_fn.forward(torch.stack(obs, dim=0).to(self.device)).view(-1, self.num_bootstrap_heads)
            else:
                q_t = torch.stack(
                    [
                        self.q_fn.forward(s[0], [np.array(s[1])])
                        for s in obs
                    ], dim=0
                ).squeeze(1)

            if self.model_type == 'mlp':
                q_tp1 = [self.q_fn_target.forward(s.to(self.device)).view(-1, self.num_bootstrap_heads) for s in
                         next_obs]
            else:
                q_tp1 = [
                    self.q_fn_target.forward(s[0], [np.array(s[1])])
                    for s in next_obs
                ]

            if self.double:

                if self.model_type == 'mlp':
                    q_tp1_online = [self.q_fn.forward(s.to(self.device)).view(-1, self.num_bootstrap_heads) for s in next_obs]
                else:
                    q_tp1_online = [
                        self.q_fn.forward(s[0], [np.array(s[1])])
                        for s in next_obs
                    ]

                q_tp1_online_idx = [
                    torch.stack(
                        [torch.argmax(q.view(-1, self.num_bootstrap_heads), dim=0),
                         torch.range(0, self.num_bootstrap_heads - 1, dtype=torch.int64).to(self.device)],
                        dim=1
                    ) for q in q_tp1_online
                ]

                v_tp1 = torch.stack(
                    [q.view(-1, self.num_bootstrap_heads)[idx[:, 0], idx[:, 1]] for q, idx in zip(q_tp1, q_tp1_online_idx)],
                    dim=0
                )

            else:
                v_tp1 = torch.stack([q.max(0)[0] for q in q_tp1], dim=0)

            q_tp1_masked = (1.0 - done.unsqueeze(1)) * v_tp1
            td_target = reward.unsqueeze(1) + self.gamma * q_tp1_masked
            td_error = (q_t - td_target).pow(2).mean()

            loss = F.smooth_l1_loss(q_t, td_target, reduction='none')

            if self.num_bootstrap_heads > 1:
                while True:
                    head_mask = torch.FloatTensor(np.random.binomial(1, 0.6, self.num_bootstrap_heads)).to(self.device)
                    if sum(head_mask).item() != 0:
                        break
                loss = loss * head_mask
                loss = loss.sum(1) / sum(head_mask)

            prios = loss.data.cpu().numpy() + self.prioritized_epsilon
            loss = loss.mul(weight).sum()

        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm(self.q_fn.parameters(), self.grad_clipping)
        self.optimizer.step()

        if self.prioritized:
            self.memory.update_priorities(
                indices, prios
            )

        if self.args.noisy:
            self.q_fn.reset_noise()
            self.q_fn_target.reset_noise()

        return td_error.data.item()

    def get_action(self, observation, epsilon, head=0):
        """
        Get action from observations

        :param observation: All possible next step.
        :param epsilon: Parameter for epsilon greedy.
        :param head: Number of bootstrap heads.
        :return: One of the next state
        """

        if random.random() > epsilon:

            steps_left = self.max_steps_per_episode - self.env.num_steps_taken
            if self.model_type == 'mlp':
                q_value = self.q_fn.forward(observation, steps_left).squeeze(1)
            else:
                q_value = self.q_fn.forward(observation, [np.array(steps_left)]).squeeze(1)

            if self.args.distribution:
                q_value = q_value * torch.linspace(self.vmin, self.vmax, self.num_bootstrap_heads).to(self.device)
                # print(q_value.shape)
                q_value = q_value.sum(1)
            else:
                q_value = q_value.gather(1, torch.LongTensor([head] * q_value.shape[0]).unsqueeze(1).to(self.device))

            action = observation[q_value.argmax().item()]

        else:
            action_space_n = len(observation)
            rand_num = random.randrange(action_space_n)
            action = observation[rand_num]

        return action

    # def generate(self):
    #     """
    #     Run the genartion process
    #     :return:
    #     """
    #     with open(self.gen_file, 'wt') as f:
    #         print('SMILES,reward', file=f)
    #         for episode in range(1, self.gen_num_episode + 1):
    #
    #             episode_start_time = time.time()
    #             state_mol, state_step = self.env.reset()
    #
    #             for step in range(self.max_steps_per_episode):
    #
    #                 state_mol, reward, sa, reward_scaled, done = self._step(
    #                     epsilon=self.gen_epsilon,
    #                     head=0,
    #                     gen=True
    #                 )
    #
    #                 if done:
    #                     print('Episode %d/%d took %gs' % (episode, self.gen_num_episode, time.time() - episode_start_time))
    #                     print('SMIELS: %s' % state_mol)
    #                     print('The reward is: %s\n' % str(reward))
    #                     print(str(state_mol) + ',' + str(reward), file=f)


class Storage(object):
    """A class to store the training result"""

    def __init__(self, keep=3):
        self._item = OrderedDict()
        self._lowest = -999
        self._highest = 999
        self._keep = keep

    def insert(self, sample):
        """
        Insert a new sample into the tracker.

        :param sample: A tuple with (mol, scaled_reward, reward, sa)
        """
        mol, scaled_reward, reward, sa = sample
        try:
            # If visited before, add one to count
            self._item[mol][3] += 1
        except:
            # If not visited before, insert it and set count to 1
            self._item[mol] = (scaled_reward, reward, sa, 1)
            self.renormalize()

    def renormalize(self):
        """
        Keep the order of the molecules w.r.t scaled reward
        """
        item_in_list = sorted(self._item.items(), key=lambda t: (t[1][0], t[1][1]), reverse=True)[:self._keep]
        self._lowest = item_in_list[-1][1][0]
        self._highest = item_in_list[0][1][0]
        self._item = OrderedDict(item_in_list)
        return None

    @property
    def content(self):
        return self._item

    @property
    def highest(self):
        return self._highest

    @property
    def lowest(self):
        return self._lowest


class MyDataParallel(nn.DataParallel):
    """Class for multi-GPU training network wrapper"""
    def __getattr__(self, item):
        try:
            return super().__getattr__(item)
        except AttributeError:
            return getattr(self.module, item)