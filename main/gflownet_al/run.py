from __future__ import print_function

import torch
import gzip
import os
import pickle
import pdb
import threading
import time
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import torch
from copy import deepcopy

from mol_mdp_ext import MolMDPExtended, BlockMoleculeDataExtended
import model_atom, model_block, model_fingerprint
import os, sys 
path_here = os.path.dirname(os.path.realpath(__file__))
sys.path.append(path_here)
from rdkit import rdBase
rdBase.DisableLog('rdApp.error')

from main.optimizer import BaseOptimizer, Objdict


class Dataset:

    def __init__(self, config, bpath, device, floatX=torch.double):
        self.train_mols = []
        self.test_mols = []
        self.train_mols_map = {}
        self.mdp = MolMDPExtended(bpath)
        self.mdp.post_init(device, config.repr_type, include_nblocks=config.include_nblocks)
        self.mdp.build_translation_table()
        self._device = device
        self.seen_molecules = set()
        self.stop_event = threading.Event()
        self.sampling_model = None
        self.sampling_model_prob = 0
        self.floatX = floatX
        self.mdp.floatX = self.floatX
        #######
        # This is the "result", here a list of (reward, BlockMolDataExt, info...) tuples
        self.sampled_mols = []

        get = lambda x, d: getattr(config, x) if hasattr(config, x) else d
        self.min_blocks = get('min_blocks', 2)
        self.max_blocks = get('max_blocks', 10)
        self.mdp._cue_max_blocks = self.max_blocks
        self.replay_mode = get('replay_mode', 'dataset')
        self.random_action_prob = get('random_action_prob', 0)
        self.R_min = get('R_min', 1e-8)
        self.do_wrong_thing = get('do_wrong_thing', False)

        self.online_mols = []
        self.max_online_mols = 1000


    def _get(self, i, dset):
        if ((self.sampling_model_prob > 0 and # don't sample if we don't have to
             np.random.uniform() < self.sampling_model_prob)
            or len(dset) < 32):
                return self._get_sample_model()
        # Sample trajectories by walking backwards from the molecules in our dataset

        # Handle possible multithreading issues when independent threads
        # add/substract from dset:
        while True:
            try:
                m = dset[i]
            except IndexError:
                i = np.random.randint(0, len(dset))
                continue
            break
        if not isinstance(m, BlockMoleculeDataExtended):
            m = m[-1]
        r = m.reward
        done = 1
        samples = []
        # a sample is a tuple (parents(s), parent actions, reward(s), s, done)
        # an action is (blockidx, stemidx) or (-1, x) for 'stop'
        # so we start with the stop action, unless the molecule is already
        # a "terminal" node (if it has no stems, no actions).
        if len(m.stems):
            samples.append(((m,), ((-1, 0),), r, m, done))
            r = done = 0
        while len(m.blocks): # and go backwards
            parents, actions = zip(*self.mdp.parents(m))
            samples.append((parents, actions, r, m, done))
            r = done = 0
            m = parents[np.random.randint(len(parents))]
        return samples

    def set_sampling_model(self, model, proxy_reward, sample_prob=0.5):
        self.sampling_model = model
        self.sampling_model_prob = sample_prob
        self.proxy_reward = proxy_reward #### only used in self._get_reward

    def _get_sample_model(self):
        m = BlockMoleculeDataExtended()
        samples = []
        max_blocks = self.max_blocks
        trajectory_stats = []
        for t in range(max_blocks):
            s = self.mdp.mols2batch([self.mdp.mol2repr(m)])
            s_o, m_o = self.sampling_model(s)
            ## fix from run 330 onwards
            if t < self.min_blocks:
                m_o = m_o * 0 - 1000 # prevent assigning prob to stop
                                     # when we can't stop
            ##
            logits = torch.cat([m_o.reshape(-1), s_o.reshape(-1)])
            cat = torch.distributions.Categorical(
                logits=logits)
            action = cat.sample().item()
            if self.random_action_prob > 0 and np.random.uniform() < self.random_action_prob:
                action = np.random.randint(int(t < self.min_blocks), logits.shape[0])

            q = torch.cat([m_o.reshape(-1), s_o.reshape(-1)])
            trajectory_stats.append((q[action].item(), action, torch.logsumexp(q, 0).item()))
            if t >= self.min_blocks and action == 0:
                r = self._get_reward(m)
                samples.append(((m,), ((-1,0),), r, m, 1))
                break
            else:
                action = max(0, action-1)
                action = (action % self.mdp.num_blocks, action // self.mdp.num_blocks)
                m_old = m
                m = self.mdp.add_block_to(m, *action)
                if len(m.blocks) and not len(m.stems) or t == max_blocks - 1:
                    # can't add anything more to this mol so let's make it
                    # terminal. Note that this node's parent isn't just m,
                    # because this is a sink for all parent transitions
                    r = self._get_reward(m)
                    if self.do_wrong_thing:
                        samples.append(((m_old,), (action,), r, m, 1))
                    else:
                        samples.append((*zip(*self.mdp.parents(m)), r, m, 1))
                    break
                else:
                    if self.do_wrong_thing:
                        samples.append(((m_old,), (action,), 0, m, 0))
                    else:
                        samples.append((*zip(*self.mdp.parents(m)), 0, m, 0))
        p = self.mdp.mols2batch([self.mdp.mol2repr(i) for i in samples[-1][0]])
        qp = self.sampling_model(p, None) #### sampling_model is model, see set_sampling_model above 
        qsa_p = self.sampling_model.index_output_by_action(
            p, qp[0], qp[1][:, 0],
            torch.tensor(samples[-1][1], device=self._device).long())
        inflow = torch.logsumexp(qsa_p.flatten(), 0).item()
        self.sampled_mols.append((r, m, trajectory_stats, inflow))
        if self.replay_mode == 'online' or self.replay_mode == 'prioritized':
            m.reward = r
            self._add_mol_to_online(r, m, inflow)
        return samples

    def _add_mol_to_online(self, r, m, inflow):
        if self.replay_mode == 'online':
            r = r + np.random.normal() * 0.01
            if len(self.online_mols) < self.max_online_mols or r > self.online_mols[0][0]:
                self.online_mols.append((r, m))
            if len(self.online_mols) > self.max_online_mols:
                self.online_mols = sorted(self.online_mols)[max(int(0.05 * self.max_online_mols), 1):]
        elif self.replay_mode == 'prioritized':
            self.online_mols.append((abs(inflow - np.log(r)), m)) ##### sampling weight 
            if len(self.online_mols) > self.max_online_mols * 1.1:
                self.online_mols = self.online_mols[-self.max_online_mols:]

    def _get_reward(self, m):
        rdmol = m.mol
        if rdmol is None:
            return self.R_min
        smi = m.smiles
        if smi in self.train_mols_map:
            return self.train_mols_map[smi].reward
        return self.proxy_reward(smi)

    def sample(self, n):
        if self.replay_mode == 'dataset': ##### random sampling from all 
            eidx = np.random.randint(0, len(self.train_mols), n)
            samples = sum((self._get(i, self.train_mols) for i in eidx), [])
        elif self.replay_mode == 'online':  ##### random sampling from online 
            eidx = np.random.randint(0, max(1,len(self.online_mols)), n)
            samples = sum((self._get(i, self.online_mols) for i in eidx), [])
        elif self.replay_mode == 'prioritized': #### weight sampling. see _add_mol_to_online above 
            if not len(self.online_mols):
                # _get will sample from the model
                samples = sum((self._get(0, self.online_mols) for i in range(n)), [])
            else:
                ##### weight sampling 
                prio = np.float32([i[0] for i in self.online_mols])  #### sampling weight, see _add_mol_to_online above 
                eidx = np.random.choice(len(self.online_mols), n, False, prio/prio.sum()) 
                samples = sum((self._get(i, self.online_mols) for i in eidx), [])
        return zip(*samples)

    def sample2batch(self, mb):
        p, a, r, s, d, *o = mb
        mols = (p, s)
        # The batch index of each parent
        p_batch = torch.tensor(sum([[i]*len(p) for i,p in enumerate(p)], []),
                               device=self._device).long()
        # Convert all parents and states to repr. Note that this
        # concatenates all the parent lists, which is why we need
        # p_batch
        p = self.mdp.mols2batch(list(map(self.mdp.mol2repr, sum(p, ()))))
        s = self.mdp.mols2batch([self.mdp.mol2repr(i) for i in s])
        # Concatenate all the actions (one per parent per sample)
        a = torch.tensor(sum(a, ()), device=self._device).long()
        # rewards and dones
        r = torch.tensor(r, device=self._device).to(self.floatX)
        d = torch.tensor(d, device=self._device).to(self.floatX)
        return (p, p_batch, a, r, s, d, mols, *o)

    def start_samplers(self, n, mbsize):
        self.ready_events = [threading.Event() for i in range(n)]
        self.resume_events = [threading.Event() for i in range(n)]
        self.results = [None] * n
        def f(idx):
            while not self.stop_event.is_set():
                try:
                    self.results[idx] = self.sample2batch(self.sample(mbsize))
                except Exception as e:
                    print("Exception while sampling:")
                    print(e)
                    self.sampler_threads[idx].failed = True
                    self.sampler_threads[idx].exception = e
                    self.ready_events[idx].set()
                    break
                self.ready_events[idx].set()
                self.resume_events[idx].clear()
                self.resume_events[idx].wait()
        self.sampler_threads = [threading.Thread(target=f, args=(i,)) for i in range(n)]
        [setattr(i, 'failed', False) for i in self.sampler_threads]
        [i.start() for i in self.sampler_threads]
        round_robin_idx = [0]
        def get():
            while True:
                idx = round_robin_idx[0]
                round_robin_idx[0] = (round_robin_idx[0] + 1) % n
                if self.ready_events[idx].is_set():
                    r = self.results[idx]
                    self.ready_events[idx].clear()
                    self.resume_events[idx].set()
                    return r
                elif round_robin_idx[0] == 0:
                    time.sleep(0.001)
        return get

    def stop_samplers_and_join(self):
        self.stop_event.set()
        if hasattr(self, 'sampler_threads'):
          while any([i.is_alive() for i in self.sampler_threads]):
            [i.set() for i in self.resume_events]
            [i.join(0.05) for i in self.sampler_threads]


def make_model(config, mdp, out_per_mol=1):
    if config.repr_type == 'block_graph':
        model = model_block.GraphAgent(nemb=config.nemb,
                                       nvec=0,
                                       out_per_stem=mdp.num_blocks,
                                       out_per_mol=out_per_mol,
                                       num_conv_steps=config.num_conv_steps,
                                       mdp_cfg=mdp,
                                       version=config.model_version)
    elif config.repr_type == 'atom_graph':
        model = model_atom.MolAC_GCN(nhid=config.nemb,
                                     nvec=0,
                                     num_out_per_stem=mdp.num_blocks,
                                     num_out_per_mol=out_per_mol,
                                     num_conv_steps=config.num_conv_steps,
                                     version=config.model_version,
                                     do_nblocks=(hasattr(config,'include_nblocks')
                                                 and config.include_nblocks), dropout_rate=0.1)
    elif config.repr_type == 'morgan_fingerprint':
        raise ValueError('reimplement me')
        model = model_fingerprint.MFP_MLP(config.nemb, 3, mdp.num_blocks, 1)
    return model


_stop = [None]
def train_model_with_proxy(config, model, proxy, dataset, num_steps=None, do_save=True):
    ##### proxy is self.oracle 
    debug_no_threads = False
    device = torch.device('cuda')

    if num_steps is None:
        num_steps = config.num_iterations + 1

    ##### target_model 
    tau = config.bootstrap_tau
    if config.bootstrap_tau > 0:
        target_model = deepcopy(model)

    if do_save:
        exp_dir = f'{config.save_path}/run_{time.time()}/'
        os.makedirs(exp_dir, exist_ok=True)

    ##### proxy (oracle) is only used here, with model 
    dataset.set_sampling_model(model, proxy, sample_prob=config.sample_prob)


    ########## save_stuff ###########
    def save_stuff():
        pickle.dump([i.data.cpu().numpy() for i in model.parameters()],
                    gzip.open(f'{exp_dir}/params.pkl.gz', 'wb'))

        pickle.dump(dataset.sampled_mols,
                    gzip.open(f'{exp_dir}/sampled_mols.pkl.gz', 'wb'))

        pickle.dump({'train_losses': train_losses,
                     'test_losses': test_losses,
                     'test_infos': test_infos,
                     'time_start': time_start,
                     'time_now': time.time(),
                     'args': config,},
                    gzip.open(f'{exp_dir}/info.pkl.gz', 'wb'))

        pickle.dump(train_infos,
                    gzip.open(f'{exp_dir}/train_info.pkl.gz', 'wb'))
    ########## save_stuff ###########

    ####### initialize setup #########
    opt = torch.optim.Adam(model.parameters(), config.learning_rate, weight_decay=config.weight_decay,
                           betas=(config.opt_beta, config.opt_beta2),
                           eps=config.opt_epsilon)

    if config.floatX == 'float32':
        tf = lambda x: torch.tensor(x, device=device).to(torch.float)
    else:
        tf = lambda x: torch.tensor(x, device=device).to(torch.double)

    mbsize = config.mbsize

    if not debug_no_threads:
        sampler = dataset.start_samplers(8, mbsize)

    last_losses = []

    def stop_everything():
        print('joining')
        dataset.stop_samplers_and_join()
    _stop[0] = stop_everything

    train_losses = []
    test_losses = []
    test_infos = []
    train_infos = []
    time_start = time.time()

    log_reg_c = config.log_reg_c
    clip_loss = tf([config.clip_loss])
    balanced_loss = config.balanced_loss
    do_nblocks_reg = False
    max_blocks = config.max_blocks
    leaf_coef = config.leaf_coef
    ####### initialize setup #########

    ######### main run ###########
    for i in range(num_steps):

        ####### 1. data ########
        if not debug_no_threads:
            r = sampler() ### sampler = dataset.start_samplers(8, mbsize)   see above. 
            for thread in dataset.sampler_threads:
                if thread.failed:
                    stop_everything()
                    pdb.post_mortem(thread.exception.__traceback__)
                    return
            p, pb, a, r, s, d, mols = r
        else:
            p, pb, a, r, s, d, mols = dataset.sample2batch(dataset.sample(mbsize)) #### see dataset.sample 
        ####### 1. data ########

        ####### 2. model ######
        # Since we sampled 'mbsize' trajectories, we're going to get roughly mbsize * H (H is variable) transitions
        ntransitions = r.shape[0]
        # state outputs
        if tau > 0:
            with torch.no_grad():
                stem_out_s, mol_out_s = target_model(s, None)
        else:
            stem_out_s, mol_out_s = model(s, None)
        # parents of the state outputs
        stem_out_p, mol_out_p = model(p, None)
        # index parents by their corresponding actions
        qsa_p = model.index_output_by_action(p, stem_out_p, mol_out_p[:, 0], a)
        # then sum the parents' contribution, this is the inflow
        exp_inflow = (torch.zeros((ntransitions,), device=device, dtype=dataset.floatX)
                      .index_add_(0, pb, torch.exp(qsa_p))) # pb is the parents' batch index
        inflow = torch.log(exp_inflow + log_reg_c)
        # sum the state's Q(s,a), this is the outflow
        exp_outflow = model.sum_output(s, torch.exp(stem_out_s), torch.exp(mol_out_s[:, 0]))
        # include reward and done multiplier, then take the log
        # we're guarenteed that r > 0 iff d = 1, so the log always works
        outflow_plus_r = torch.log(log_reg_c + r + exp_outflow * (1-d))
        if do_nblocks_reg:
            losses = _losses = ((inflow - outflow_plus_r) / (s.nblocks * max_blocks)).pow(2)
        else:
            losses = _losses = (inflow - outflow_plus_r).pow(2)
        if clip_loss > 0:
            ld = losses.detach()
            losses = losses / ld * torch.minimum(ld, clip_loss)

        term_loss = (losses * d).sum() / (d.sum() + 1e-20)
        flow_loss = (losses * (1-d)).sum() / ((1-d).sum() + 1e-20)
        if balanced_loss:
            loss = term_loss * leaf_coef + flow_loss
        else:
            loss = losses.mean()
        opt.zero_grad()
        loss.backward(retain_graph=(not i % 50))

        _term_loss = (_losses * d).sum() / (d.sum() + 1e-20)
        _flow_loss = (_losses * (1-d)).sum() / ((1-d).sum() + 1e-20)
        last_losses.append((loss.item(), term_loss.item(), flow_loss.item()))
        train_losses.append((loss.item(), _term_loss.item(), _flow_loss.item(),
                             term_loss.item(), flow_loss.item()))

        ###### logging ###### 
        if not i % 50:
            train_infos.append((
                _term_loss.data.cpu().numpy(),
                _flow_loss.data.cpu().numpy(),
                exp_inflow.data.cpu().numpy(),
                exp_outflow.data.cpu().numpy(),
                r.data.cpu().numpy(),
                mols[1],
                [i.pow(2).sum().item() for i in model.parameters()],
                torch.autograd.grad(loss, qsa_p, retain_graph=True)[0].data.cpu().numpy(),
                torch.autograd.grad(loss, stem_out_s, retain_graph=True)[0].data.cpu().numpy(),
                torch.autograd.grad(loss, stem_out_p, retain_graph=True)[0].data.cpu().numpy(),
            ))
        ###### logging ###### 

        ###### optimizer #####
        if config.clip_grad > 0:
            torch.nn.utils.clip_grad_value_(model.parameters(),
                                            config.clip_grad)
        opt.step()
        model.training_steps = i + 1
        ###### optimizer #####

        ##### update target_model #######
        if tau > 0:
            for _a,b in zip(model.parameters(), target_model.parameters()):
                b.data.mul_(1-tau).add_(tau*_a)

        if not i % 1000 and do_save:
            save_stuff()

    stop_everything()
    if do_save:
        save_stuff()
    return model


class GFlowNet_AL_Optimizer(BaseOptimizer):

    def __init__(self, args=None):
        super().__init__(args)
        self.model_name = "gflownet_al"

    def _optimize(self, oracle, config):

        self.oracle.assign_evaluator(oracle)
        config = Objdict(config)
        bpath = os.path.join(path_here, 'data/blocks_PDB_105.json')
        device = torch.device('cuda')

        proxy_repr_type = config.proxy_repr_type
        repr_type = config.repr_type
        reward_exp = config.reward_exp
        reward_norm = config.reward_norm
        rews = []
        smis = []
        actors = [_SimDockLet.remote(tmp_dir)
                        for i in range(10)]
        pool = ray.util.ActorPool(actors)
        config.repr_type = proxy_repr_type
        config.replay_mode = "dataset"
        config.reward_exp = 1
        config.reward_norm = 1
        proxy_dataset = ProxyDataset(args, bpath, device, floatX=torch.float)
        proxy_dataset.load_h5("data/docked_mols.h5", config, num_examples=config.num_init_examples)
        rews.append(proxy_dataset.rews)
        smis.append([mol.smiles for mol in proxy_dataset.train_mols])
        rew_max = np.max(proxy_dataset.rews)
        print(np.max(proxy_dataset.rews))
        exp_dir = f'{config.save_path}/proxy_{config.array}_{config.run}/'
        os.makedirs(exp_dir, exist_ok=True)

        print(len(proxy_dataset.train_mols), 'train mols')
        print(len(proxy_dataset.test_mols), 'test mols')
        print(config)
        # import pdb;pdb.set_trace()
        proxy = Proxy(config, bpath, device)
        mdp = proxy_dataset.mdp
        train_metrics = []
        metrics = []
        proxy.train(proxy_dataset)

        for i in range(config.num_outer_loop_iters):
            print(f"Starting step: {i}")
            # Initialize model and dataset for training generator
            config.sample_prob = 1
            config.repr_type = repr_type
            config.reward_exp = reward_exp
            config.reward_norm = reward_norm
            config.replay_mode = "online"
            gen_model_dataset = GenModelDataset(config, bpath, device)
            model = make_model(config, gen_model_dataset.mdp)

            if config.floatX == 'float64':
                model = model.double()
            model.to(device)
            # train model with with proxy
            print(f"Training model: {i}")
            model, gen_model_dataset, training_metrics = train_generative_model(config, model, proxy, gen_model_dataset, do_save=False)

            print(f"Sampling mols: {i}")
            # sample molecule batch for generator and update dataset with docking scores for sampled batch
            _proxy_dataset, r, s, batch_metrics = sample_and_update_dataset(config, model, proxy_dataset, gen_model_dataset, pool)
            print(f"Batch Metrics: dists_mean: {batch_metrics['dists_mean']}, dists_sum: {batch_metrics['dists_sum']}, reward_mean: {batch_metrics['reward_mean']}, reward_max: {batch_metrics['reward_max']}")
            rews.append(r)
            smis.append(s)
            config.sample_prob = 0
            config.repr_type = proxy_repr_type
            config.replay_mode = "dataset"
            config.reward_exp = 1
            config.reward_norm = 1

            train_metrics.append(training_metrics)
            metrics.append(batch_metrics)

            proxy_dataset = ProxyDataset(config, bpath, device, floatX=torch.float)
            proxy_dataset.train_mols.extend(_proxy_dataset.train_mols)
            proxy_dataset.test_mols.extend(_proxy_dataset.test_mols)

            proxy = Proxy(config, bpath, device)
            mdp = proxy_dataset.mdp

            pickle.dump({'train_metrics': train_metrics,
                        'batch_metrics': metrics,
                        'rews': rews,
                        'smis': smis,
                        'rew_max': rew_max,
                        'args': config},
                        gzip.open(f'{exp_dir}/info.pkl.gz', 'wb'))

            print(f"Updating proxy: {i}")
            # update proxy with new data
            proxy.train(proxy_dataset)