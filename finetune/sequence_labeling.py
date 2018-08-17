import tensorflow as tf
from sklearn.model_selection import train_test_split
import pandas as pd

from finetune.base import BaseModel
from finetune.encoding import EncodedOutput, ArrayEncodedOutput
from finetune.target_encoders import SequenceLabelingEncoder
from finetune.network_modules import sequence_labeler
from finetune.utils import indico_to_finetune_sequence, finetune_to_indico_sequence, sequence_decode


class SequenceLabeler(BaseModel):
    """ 
    Labels each token in a sequence as belonging to 1 of N token classes.
    
    :param config: A :py:class:`finetune.config.Settings` object or None (for default config).
    :param \**kwargs: key-value pairs of config items to override.
    """
        
    def finetune(self, X, Y=None, batch_size=None):
        """
        :param X: A list of text snippets. Format: [batch_size]
        :param Y: A list of lists of annotations. Format: [batch_size, n_annotations], where each annotation is of the form:
            {'start': 0, 'end': 5, 'label': 'class', 'text': 'sample text'}
        :param batch_size: integer number of examples per batch. When N_GPUS > 1, this number
                           corresponds to the number of training examples provided to each GPU.
        :param val_size: Float fraction or int number that represents the size of the validation set.
        :param val_interval: The interval for which validation is performed, measured in number of steps.
        """
        fit_target_model = (Y is not None)
        X, Y = indico_to_finetune_sequence(X, Y, none_value="<PAD>")
        arr_encoded = self._text_to_ids(X, Y=Y)
        targets = arr_encoded.labels if fit_target_model else None
        return self._training_loop(arr_encoded, Y=targets, batch_size=batch_size)

    def predict(self, X, max_length=None):
        """
        Produces a list of most likely class labels as determined by the fine-tuned model.

        :param X: A list / array of text, shape [batch]
        :param max_length: the number of tokens to be included in the document representation.
                           Providing more than `max_length` tokens as input will result in truncatindiion.
        :returns: list of class labels.
        """
        doc_subseqs, _ = indico_to_finetune_sequence(X)

        max_length = max_length or self.config.max_length
        chunk_size = max_length - 2
        step_size = chunk_size // 3
        
        arr_encoded = self._text_to_ids(doc_subseqs)
        labels = self._predict(doc_subseqs, max_length=max_length)
        all_subseqs = []
        all_labels = []

        doc_idx = -1
        
        
        for chunk_idx, (label_seq, position_seq) in enumerate(zip(labels, arr_encoded.char_locs)):
            start_of_doc = arr_encoded.token_ids[chunk_idx][0][0] == self.encoder.start
            end_of_doc = (
                chunk_idx + 1 >= len(arr_encoded.char_locs) or 
                arr_encoded.token_ids[chunk_idx + 1][0][0] == self.encoder.start
            )

            """
            Chunk idx for prediction.  Dividers at `step_size` increments.
            [  1  |  1  |  2  |  3  |  3  ]
            """
            if start_of_doc:
                # if this is the first chunk in a document, start accumulating from scratch
                doc_subseqs = []
                doc_labels = []
                doc_idx += 1
                start_of_token = 0
                if not end_of_doc:
                    # predict only on first two thirds
                    label_seq, position_seq = label_seq[:step_size*2], position_seq[:step_size*2]
            else:
                if end_of_doc:
                    # predict on the rest of sequence
                    label_seq, position_seq = label_seq[step_size:], position_seq[step_size:]
                else:
                    # predict only on middle third
                    label_seq, position_seq = label_seq[step_size:step_size*2], position_seq[step_size:step_size*2]

            for label, position in zip(label_seq, position_seq):
                if position == -1:
                    # indicates padding / special tokens
                    continue

                # if there are no current subsequence
                # or the current subsequence has the wrong label
                if not doc_subseqs or label != doc_labels[-1]:
                    # start new subsequence
                    doc_subseqs.append(X[doc_idx][start_of_token:position])
                    doc_labels.append(label)
                else:
                    # continue appending to current subsequence
                    doc_subseqs[-1] += X[doc_idx][start_of_token:position]

                start_of_token = position

            if end_of_doc:
                # last chunk in a document
                all_subseqs.append(doc_subseqs)
                all_labels.append(doc_labels)

        _, doc_annotations = finetune_to_indico_sequence(
            raw_texts=X,
            subseqs=all_subseqs,
            labels=all_labels,
            subtoken_predictions=self.config.subtoken_predictions
        )

        return doc_annotations

    def featurize(self, X, max_length=None):
        """
        Embeds inputs in learned feature space. Can be called before or after calling :meth:`finetune`.

        :param Xs: An iterable of lists or array of text, shape [batch, n_inputs, tokens]
        :param max_length: the number of tokens to be included in the document representation.
                           Providing more than `max_length` tokens as input will result in truncation.
        :returns: np.array of features of shape (n_examples, embedding_size).
        """
        return self._featurize(X, max_length=max_length)

    def predict_proba(self, X, max_length=None):
        """
        Produces a list of most likely class labels as determined by the fine-tuned model.

        :param X: A list / array of text, shape [batch]
        :param max_length: the number of tokens to be included in the document representation.
                           Providing more than `max_length` tokens as input will result in truncatindiion.
        :returns: list of class labels.
        """
        doc_subseqs, _ = indico_to_finetune_sequence(X)
        arr_encoded = self._text_to_ids(doc_subseqs)
        batch_probas = self._predict_proba(doc_subseqs, max_length=max_length)
        
        max_length = max_length or self.config.max_length
        chunk_size = max_length - 2
        step_size = chunk_size // 3

        result = []
        for chunk_idx, (token_seq, proba_seq) in enumerate(zip(arr_encoded.tokens, batch_probas)):
            start_of_doc = (arr_encoded.token_ids[chunk_idx][0][0] == self.encoder.start)
            end_of_doc = (
                chunk_idx + 1 >= len(arr_encoded.char_locs) or 
                arr_encoded.token_ids[chunk_idx + 1][0][0] == self.encoder.start
            )
            if start_of_doc:
                seq_result = []
                if not end_of_doc:
                    token_seq, proba_seq = token_seq[:step_size*2], proba_seq[:step_size*2] 
            else:
                if end_of_doc:
                    token_seq, proba_seq = token_seq[step_size:], proba_seq[step_size:]
                else:
                    # we've already predicted on the first half of this seq as part of the prev. chunk
                    token_seq, proba_seq = token_seq[step_size:step_size*2], proba_seq[step_size:step_size*2]

            for token, proba_t in zip(token_seq, proba_seq):
                seq_result.append((
                    token,
                    dict(zip(self.label_encoder.classes_, proba_t))
                ))

            

            if end_of_doc:
                result.append(seq_result)
            
        return result

    def _format_for_encoding(self, Xs):
        """
        Pad out each example to make it clear there is only a single field
        """
        return [[X] for X in Xs]

    def _target_placeholder(self, target_dim=None):
        return tf.placeholder(tf.int32, [None, self.config.max_length])  # classification targets

    def _target_encoder(self):
        return SequenceLabelingEncoder()

    def _target_model(self, featurizer_state, targets, n_outputs, train=False, reuse=None, **kwargs):
        return sequence_labeler(
            hidden=featurizer_state['sequence_features'],
            targets=targets, 
            n_targets=n_outputs,
            dropout_placeholder=self.do_dropout,
            config=self.config,
            train=train, 
            reuse=reuse, 
            **kwargs
        )
    
    def _predict_op(self, logits, **kwargs):
        label_idxs, _ = sequence_decode(logits, kwargs.get("transition_matrix"))
        return label_idxs

    def _predict_proba_op(self, logits, **kwargs):
        _, label_probas = sequence_decode(logits, kwargs.get("transition_matrix"))
        return label_probas
