"""
Copyright 2017 Neural Networks and Deep Learning lab, MIPT

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import re

import numpy as np
from typing import Type

from deeppavlov.core.common.registry import register
from deeppavlov.core.models.inferable import Inferable
from deeppavlov.core.models.trainable import Trainable
from deeppavlov.core.common.errors import ConfigError
from deeppavlov.models.embedders.fasttext_embedder import FasttextEmbedder
from deeppavlov.models.encoders.bow import BoW_encoder
from deeppavlov.models.classifiers.intents.intent_model import KerasIntentModel
from deeppavlov.models.ner.slotfill import DstcSlotFillingNetwork
from deeppavlov.models.tokenizers.spacy_tokenizer import SpacyTokenizer
from deeppavlov.models.trackers.default_tracker import DefaultTracker
from deeppavlov.skills.go_bot.metrics import DialogMetrics
from deeppavlov.skills.go_bot.network import GoalOrientedBotNetwork
from deeppavlov.skills.go_bot.templates import Templates, DualTemplate
from deeppavlov.core.common.attributes import check_attr_true
from deeppavlov.core.common.log import get_logger


log = get_logger(__name__)


@register("go_bot")
class GoalOrientedBot(Inferable, Trainable):
    def __init__(self, template_path, vocabs,
                 template_type: Type = DualTemplate,
                 bow_encoder: Type = BoW_encoder,
                 tokenizer: Type = SpacyTokenizer,
                 tracker: Type = DefaultTracker,
                 network: Type = GoalOrientedBotNetwork,
                 embedder=None,
                 slot_filler=None,
                 intent_classifier=None,
                 use_action_mask=False,
                 debug=False,
                 num_epochs=200,
                 val_patience=10,
                 train_now=False,
                 save_path=None,
                 **kwargs):

        super().__init__(save_path=save_path, train_now=train_now, mode=kwargs['mode'])

        self.episode_done = True
        self.use_action_mask = use_action_mask
        self.debug = debug
        self.slot_filler = slot_filler
        self.intent_classifier = intent_classifier
        self.bow_encoder = bow_encoder
        self.embedder = embedder
        self.tokenizer = tokenizer
        self.tracker = tracker
        self.network = network
        self.word_vocab = vocabs['word_vocab']
        self.num_epochs = num_epochs
        self.val_patience = val_patience

        self.templates = Templates(template_type).load(template_path)
        log.info("[using {} templates from `{}`]".format(len(self.templates), template_path))

        # intialize parameters
        self.db_result = None
        self.n_actions = len(self.templates)
        self.n_intents = 0
        if hasattr(self.intent_classifier, 'n_classes'):
            self.n_intents = self.intent_classifier.n_classes
        self.prev_action = np.zeros(self.n_actions, dtype=np.float32)

        # initialize metrics
        self.metrics = DialogMetrics(self.n_actions)

        # opt = {
        #    'action_size': self.n_actions,
        #    'obs_size': 4 + len(self.word_vocab) + self.embedder.dim +\
        #    self.tracker.num_features + self.n_actions + self.n_intents
        # }
        # self.network = GoalOrientedBotNetwork(opt)

    def _encode_context(self, context, db_result=None):
        # tokenize input
        tokenized = ' '.join(self.tokenizer.infer(context)).strip()
        if self.debug:
            log.debug("Text tokens = `{}`".format(tokenized))

        # Bag of words features
        bow_features = self.bow_encoder.infer(tokenized, self.word_vocab)
        bow_features = bow_features.astype(np.float32)

        # Embeddings
        emb_features = []
        if hasattr(self.embedder, 'infer'):
            emb_features = self.embedder.infer(tokenized, mean=True)

        # Intent features
        intent_features = []
        if hasattr(self.intent_classifier, 'infer'):
            intent_features = self.intent_classifier.infer(tokenized,
                                                           predict_proba=True).ravel()
            if self.debug:
                log.debug("Predicted intent = `{}`".format(
                    self.intent_classifier.infer(tokenized)))

        # Text entity features
        if hasattr(self.slot_filler, 'infer'):
            self.tracker.update_state(self.slot_filler.infer(tokenized))
            if self.debug:
                log.debug("Slot vals: {}".format(str(self.slot_filler.infer(tokenized))))

        state_features = self.tracker.infer()

        # Other features
        context_features = np.array([(db_result == {}) * 1.,
                                     (self.db_result == {}) * 1.],
                                    dtype=np.float32)

        if self.debug:
            debug_msg = "num bow features = {}, " \
                        "num emb features = {}, " \
                        "num intent features = {}, " \
                        "num state features = {}, " \
                        "num context features = {}, " \
                        "prev_action shape = {}".format(len(bow_features),
                                                        len(emb_features),
                                                        len(intent_features),
                                                        len(state_features),
                                                        len(context_features),
                                                        len(self.prev_action))

            log.debug(debug_msg)

        return np.hstack((bow_features, emb_features, intent_features,
                          state_features, context_features, self.prev_action))

    def _encode_response(self, act):
        return self.templates.actions.index(act)

    def _decode_response(self, action_id):
        """
        Convert action template id and entities from tracker
        to final response.
        """
        template = self.templates.templates[int(action_id)]

        slots = self.tracker.get_state()
        if self.db_result is not None:
            for k, v in self.db_result.items():
                slots[k] = str(v)

        return template.generate_text(slots)

    def _action_mask(self):
        action_mask = np.ones(self.n_actions, dtype=np.float32)
        if self.use_action_mask:
            # TODO: non-ones action mask
            for a_id in range(self.n_actions):
                tmpl = str(self.templates.templates[a_id])
                for entity in re.findall('#{}', tmpl):
                    if entity not in self.tracker.get_state() \
                            and entity not in (self.db_result or {}):
                        action_mask[a_id] = 0
        return action_mask

    @check_attr_true('train_now')
    def train_on_batch(self, batch):
        for dialog in zip(*batch):
            self.reset()
            d_features, d_actions, d_masks = [], [], []
            for context, response in zip(*dialog):
                features = self._encode_context(context['text'],
                                                context.get('db_result'))
                if context.get('db_result') is not None:
                    self.db_result = context['db_result']
                d_features.append(features)

                action_id = self._encode_response(response['act'])
                # previous action is teacher-forced here
                self.prev_action *= 0.
                self.prev_action[action_id] = 1.
                d_actions.append(action_id)

                d_masks.append(self._action_mask())

            self.network.train(d_features, d_actions, d_masks)

    @check_attr_true('train_now')
    def train(self, data):
        
        if self.network.train_now is False:
            raise ConfigError("It looks like 'train_now' of mother model is True, while"
                              "`train_now` of submodel is False. Set `train_now` of submodel"
                              "to True.")

        print('\n:: training started')

        curr_patience = self.val_patience
        best_valid_accuracy = 0.
        # TODO: in case val_patience is off, save model {val_patience} steps before
        for j in range(self.num_epochs):

            tr_data = data.batch_generator(1, 'train', shuffle=False)

            self.reset_metrics()

            for batch in tr_data:
                for dialog in zip(*batch):

                    self.reset()
                    self.metrics.n_dialogs += 1
                    d_features, d_actions, d_masks = [], [], []

                    for context, response in zip(*dialog):
                        features = self._encode_context(context['text'],
                                                        context.get('db_result'))
                        if context.get('db_result') is not None:
                            self.db_result = context['db_result']
                        d_features.append(features)

                        action_id = self._encode_response(response['act'])
                        # previous action is teacher-forced here
                        self.prev_action *= 0.
                        self.prev_action[action_id] = 1.
                        d_actions.append(action_id)

                        d_masks.append(self._action_mask())

                    loss, d_preds = self.network.train(d_features, d_actions, d_masks)

                    for pred_id, action_id in zip(d_preds, d_actions):
                        pred = ""
                        # TODO: decoding is using wrong state
                        #self._decode_response(pred_id).lower()
                        true = self.tokenizer.infer(response['text'].lower().split())

                        # update metrics
                        self.metrics.n_examples += 1
                        self.metrics.train_loss += loss
                        self.metrics.conf_matrix[pred_id, action_id] += 1
                        #self.metrics.n_corr_examples += int(pred == true)
                        if self.debug and ((pred == true) != (pred_id == action_id)):
                            print("Slot filling problem: ")
                            print("Pred = {}: {}".format(pred_id, pred))
                            print("True = {}: {}".format(action_id, true))
                            #print("State =", self.tracker.get_state())
                            #print("db_result =", self.db_result)
                            # TODO: update dialog metrics
                    
            print('\n\n:: {}.train {}'.format(j + 1, self.metrics.report()))

            train_data = data.batch_generator(1, 'train')
            eval_data = data.batch_generator(1, 'valid')
            #train_data = data.iter_all('train')
            train_metrics = self.evaluate(train_data)
            print(':: {}.train {}'.format(j + 1, train_metrics.report()))
            valid_metrics = self.evaluate(eval_data)
            print(':: {}.valid {}'.format(j + 1, valid_metrics.report()))

            if valid_metrics.action_accuracy < best_valid_accuracy:
                curr_patience -= 1
                print(":: patience decreased by 1, is equal to {}".format(curr_patience))
            else:
                if curr_patience != self.val_patience:
                    curr_patience = self.val_patience
                    print(":: patience is equal to {}".format(curr_patience))
                best_valid_accuracy = valid_metrics.action_accuracy
            if curr_patience < 1:
                print("\n:: patience is over, stopped training\n")
                break
        else:
            print("\n:: stopping because max number of epochs encountered\n")
        self.save()

    def infer_on_batch(self, xs):
        return [self._infer_dialog(x) for x in xs]

    def _infer(self, context, db_result=None, prob=False):
        probs = self.network.infer(
            self._encode_context(context, db_result),
            self._action_mask(),
            prob=True
        )
        pred_id = np.argmax(probs)
        if db_result is not None:
            self.db_result = db_result

        # one-hot encoding seems to work better then probabilities
        if prob:
            self.prev_action = probs
        else:
            self.prev_action *= 0
            self.prev_action[pred_id] = 1

        return self._decode_response(pred_id)

    def _infer_dialog(self, contexts):
        self.reset()
        res = []
        for context in contexts:
            if context.get('prev_resp_act') is not None:
                action_id = self._encode_response(context.get('prev_resp_act'))
                # previous action is teacher-forced
                self.prev_action *= 0.
                self.prev_action[action_id] = 1.

            res.append(self._infer(context['text'], context.get('db_result')))
        return res

    def infer(self, x):
        if isinstance(x, str):
            return self._infer(x)
        return self.infer_on_batch(x)

    def reset(self):
        self.tracker.reset_state()
        self.db_result = None
        self.prev_action = np.zeros(self.n_actions, dtype=np.float32)
        self.network.reset_state()

    def report(self):
        return self.metrics.report()

    def reset_metrics(self):
        self.metrics.reset()

    def save(self):
        """Save the parameters of the model to a file."""
        self.network.save()

    def shutdown(self):
        self.network.shutdown()
        self.slot_filler.shutdown()

    def load(self):
        pass
