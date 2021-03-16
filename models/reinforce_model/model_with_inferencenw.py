from transformers import RobertaForSequenceClassification

import torch
import torch.nn as nn
import torch.nn.functional as F
from models.reinforce_model.prior_posterior_models import PriorRobertaModel, InferenceRobertaModel
from models.reinforce_model.dataset import EFFECTS

TRAINING_TYPE_MARGINALIZE = 'marginalize'
TRAINING_TYPE_REINFORCE = 'reinforce'


class LatentVariableInferenceModel(nn.Module):
    def __init__(self,
                 args,
                 generator_class):
        super().__init__()

        self.args = args
        if args.prior_model == 'bow':
            raise NotImplementedError
        elif args.prior_model == 'roberta':
            self.prior_model = PriorRobertaModel(args)
            self.inference_model = InferenceRobertaModel(args)
        else:
            raise Exception('Invalid prior model')

        self.gpt2_model = generator_class.from_pretrained(args.model_checkpoint)
        self.criterion_lm = torch.nn.CrossEntropyLoss(ignore_index=-100, reduction='none')
        self.criterion_mc = torch.nn.CrossEntropyLoss(reduction='none')

        if args.training_type == 'reinforce':
            self.training_type = TRAINING_TYPE_REINFORCE
        else:
            self.training_type = TRAINING_TYPE_MARGINALIZE  # default
            raise NotImplementedError

        print('Model loaded with training type {}'.format(self.training_type))

        assert self.training_type in [TRAINING_TYPE_REINFORCE, TRAINING_TYPE_MARGINALIZE]
        self.running_mean = None  # -- todo: maybe init as 0?
        self.use_baseline = args.use_baseline
        self.moving_avg_ratio = args.moving_avg_ratio
        self.reinforce_loss_coef = args.reinforce_loss_coef
        self.entropy_regularize_prior_wt = args.entropy_regularize_prior_wt

    def forward(
            self,
            input_ids,
            token_type_ids,
            persona=None,
            history=None,
            mc_token_ids=None,
            lm_labels=None,
            mc_labels=None,
            generate=False,
            interpret=False,
            kl_weight = 1.0,  #TODO - annealing from 0 to 1
            **kwargs):
        '''
        persona: B x P x T
        input_ids: B x P x C x T
        mc_token_ids:
        lm_labels: B x P x C x T
        mc_labels: B
        token_type_ids: B x P x C x T
        '''

        effects = kwargs.get('effects', None)

        sampler_model = self.inference_model

        if not generate:

            z_given_h_and_x = sampler_model.get_prob_z_given_H_and_x(mc_token_ids, persona, history, effects)  # B x P
            z_given_h = self.prior_model.get_prob_z_given_H(persona, history, effects)  # B x P

            log_probs_lm = []
            log_probs_mc = []

            # z_iterator = range(input_ids.shape[1])
            if self.training_type == TRAINING_TYPE_MARGINALIZE:
                z_iterator = range(input_ids.shape[1])
                raise NotImplementedError
            elif self.training_type == TRAINING_TYPE_REINFORCE:
                action, logprob_action = sampler_model.sample(z_given_h_and_x)

                z_iterator = [action]  # in case of reinforce, do fwd for only one value of z
                # z_given_h = z_given_h.detach()  # do not update prior through log likelihood since we are not marginalizing. we will instead update it through reinforce

            for i in z_iterator:

                if self.training_type == TRAINING_TYPE_MARGINALIZE:
                    lm_logits, mc_logits, *_ = self.gpt2_model(
                        input_ids[:, i, ...].contiguous(),
                        token_type_ids=token_type_ids[:, i, ...].contiguous(),
                        mc_token_ids=mc_token_ids[:, i, ...].contiguous(),
                    )
                    lm_labels_persona = lm_labels[:, i, ...]
                    mc_labels_persona = mc_labels[:, i, ...]

                elif self.training_type == TRAINING_TYPE_REINFORCE:
                    input_ids = torch.cat(
                        [torch.index_select(ip, 0, ind).unsqueeze(0) for ip, ind in zip(input_ids, i)])
                    token_type_ids = torch.cat(
                        [torch.index_select(ip, 0, ind).unsqueeze(0) for ip, ind in zip(token_type_ids, i)])
                    mc_token_ids = torch.cat(
                        [torch.index_select(ip, 0, ind).unsqueeze(0) for ip, ind in zip(mc_token_ids, i)])
                    lm_labels_persona = torch.cat(
                        [torch.index_select(ip, 0, ind).unsqueeze(0) for ip, ind in zip(lm_labels, i)])
                    mc_labels_persona = torch.cat(
                        [torch.index_select(ip, 0, ind).unsqueeze(0) for ip, ind in zip(mc_labels, i)])

                    lm_logits, mc_logits, *_ = self.gpt2_model(
                        input_ids,
                        token_type_ids=token_type_ids,
                        mc_token_ids=mc_token_ids,
                    )

                # LM
                lm_logits_flat_shifted = lm_logits[..., :-1, :].contiguous().view(-1, lm_logits.size(-1))
                lm_labels_flat_shifted = lm_labels_persona[..., 1:].contiguous().view(-1)

                num_labels = (lm_labels_persona[:, 0, 0, :] != -100).sum(-1)  # B

                ll_lm = -1 * self.criterion_lm(lm_logits_flat_shifted, lm_labels_flat_shifted)  # B x C x T
                ll_lm = ll_lm.view(lm_labels.size(0), -1).sum(-1)  # B

                log_prob_x_z_given_h = ll_lm
                if self.training_type == TRAINING_TYPE_MARGINALIZE:
                    log_prob_x_z_given_h += torch.log(z_given_h[:, i])  # B

                log_probs_lm.append(log_prob_x_z_given_h / num_labels)  # This line is trhowing error

                # # MC
                # ll_mc = -1.0 * self.criterion_mc(mc_logits.view(-1, mc_logits.size(-1)), mc_labels_persona.view(-1))
                # ll_mc = ll_mc.view(mc_labels.size(0), -1).sum(-1)

                # log_prob_x_given_z_h_mc = ll_mc + torch.log(z_given_h[:, i])  # B
                # log_probs_mc.append(log_prob_x_given_z_h_mc)

            if self.training_type == TRAINING_TYPE_MARGINALIZE:
                # LM
                log_probs_lm = torch.stack(log_probs_lm).T  # B x P
                if interpret:
                    return log_probs_lm
                log_sum_exp_lm = torch.logsumexp(log_probs_lm, dim=1)  # logsumexp,  B
                total_loss_lm = -1.0 * log_sum_exp_lm.mean()
                loss_prior, reinforce_loss_lm = torch.Tensor([0.0]).to(self.args.device), torch.Tensor([0.0]).to(
                    self.args.device)

            elif self.training_type == TRAINING_TYPE_REINFORCE:
                # not when using reinforce, loss_lm is not log p(x) but log p(x|z=action) -- so be careful when compuing the perplexity
                # LM
                # log_probs_lm: P=1 values for B=batch_size. pick the first and only value
                log_probs_lm = log_probs_lm[0]  # log_probs_lm:B
                log_sum_exp_lm = log_probs_lm  # B
                loss_lm = -1.0 * log_sum_exp_lm.mean()
                # reward: we want to reward those actions which lead to higher
                rewards = log_sum_exp_lm.detach()  # important to detach -> to not update the conditional model
                track_rewards = rewards.mean()
                if self.use_baseline:
                    if not self.running_mean:
                        self.running_mean = rewards.mean().detach()  # 1
                    else:
                        ratio = 0.99
                        self.running_mean = ratio * self.running_mean + (1.0 - ratio) * rewards.mean()
                    rewards = rewards - self.running_mean.detach()  # B

                # todo - should do some sort of baseline computation for stable reinforce training
                loss_prior = - logprob_action * rewards  # B
                loss_prior = loss_prior.mean()  # B
                # sum the two losses. todo - use a weight on reinforce
                total_loss_lm = loss_lm + self.reinforce_loss_coef * loss_prior
                elbo_loss_tracking = loss_lm

                if self.entropy_regularize_prior_wt > 0.0:
                    if self.training:  # add entropy term only in train mode
                        # TODO: try with the inference network
                        # entropy = self.prior_model.entropy(z_given_h)
                        entropy = self.inference_model.entropy(z_given_h_and_x)
                        # print("***** entropy = ", entropy)
                        loss_prior += (-self.entropy_regularize_prior_wt * entropy)  # low entropy is bad


            # compute KL term
            kl_loss = self.compute_kl_loss(z_given_h_and_x, z_given_h)
            total_loss_lm += kl_weight * kl_loss
            elbo_loss_tracking += kl_loss

            # # MC
            # log_probs_mc = torch.stack(log_probs_mc).T
            # log_sum_exp_mc = torch.logsumexp(log_probs_mc, dim=1)  # logsumexp
            # loss_mc = -1.0 * log_sum_exp_mc.mean()
            loss_mc = torch.Tensor([0.0]).to(self.args.device)

            return total_loss_lm, loss_mc, loss_prior, loss_lm, num_labels, track_rewards, kl_loss, elbo_loss_tracking

        if generate:
            lm_logits = self.gpt2_model(
                input_ids=input_ids,
                token_type_ids=token_type_ids,
            )

            return lm_logits

    def compute_kl_loss(self, posterior, prior):
        # TODO: can get numerically unstable
        log_posterior = torch.log(posterior) # BS * P
        log_prior = torch.log(prior) # BS * P
        kl_loss = torch.mean( torch.sum(posterior * (log_posterior - log_prior), dim=1 ), dim=0 )
        return kl_loss
