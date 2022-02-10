import logging
from pathlib import Path
from typing import List, Optional, Set, Tuple, Union

import torch

import flair.embeddings
import flair.nn
from flair.data import RelationLabel, Sentence, Span
from flair.file_utils import cached_path

log = logging.getLogger("flair")


class RelationExtractor(flair.nn.DefaultClassifier[Sentence]):
    def __init__(
        self,
        embeddings: Union[flair.embeddings.TokenEmbeddings],
        label_type: str,
        entity_label_type: str,
        train_on_gold_pairs_only: bool = False,
        entity_pair_filters: List[Tuple[str, str]] = None,
        pooling_operation: str = "first_last",
        dropout_value: float = 0.0,
        locked_dropout_value: float = 0.1,
        word_dropout_value: float = 0.0,
        **classifierargs,
    ):
        """
        Initializes a RelationClassifier
        :param document_embeddings: embeddings used to embed each data point
        :param label_dictionary: dictionary of labels you want to predict
        :param beta: Parameter for F-beta score for evaluation and training annealing
        :param loss_weights: Dictionary of weights for labels for the loss function
        (if any label's weight is unspecified it will default to 1.0)
        """

        # pooling operation to get embeddings for entites
        self.pooling_operation = pooling_operation
        relation_representation_length = 2 * embeddings.embedding_length
        if self.pooling_operation == "first_last":
            relation_representation_length *= 2

        super(RelationExtractor, self).__init__(**classifierargs, final_embedding_size=relation_representation_length)

        # set embeddings
        self.embeddings: flair.embeddings.TokenEmbeddings = embeddings

        # set relation and entity label types
        self._label_type = label_type
        self.entity_label_type = entity_label_type

        # whether to use gold entity pairs, and whether to filter entity pairs by type
        self.train_on_gold_pairs_only = train_on_gold_pairs_only
        if entity_pair_filters is not None:
            self.entity_pair_filters: Optional[Set[Tuple[str, str]]] = set(entity_pair_filters)
        else:
            self.entity_pair_filters = None

        # init dropouts
        self.dropout_value = dropout_value
        self.dropout = torch.nn.Dropout(dropout_value)
        self.locked_dropout_value = locked_dropout_value
        self.locked_dropout = flair.nn.LockedDropout(locked_dropout_value)
        self.word_dropout_value = word_dropout_value
        self.word_dropout = flair.nn.WordDropout(word_dropout_value)

        self.to(flair.device)

    def add_entity_markers(self, sentence, span_1, span_2):

        text = ""

        entity_one_is_first = None
        offset = 0
        for token in sentence:
            if token == span_2[0]:
                if entity_one_is_first is None:
                    entity_one_is_first = False
                offset += 1
                text += " <e2>"
                span_2_startid = offset
            if token == span_1[0]:
                offset += 1
                text += " <e1>"
                if entity_one_is_first is None:
                    entity_one_is_first = True
                span_1_startid = offset

            text += " " + token.text

            if token == span_1[-1]:
                offset += 1
                text += " </e1>"
            if token == span_2[-1]:
                offset += 1
                text += " </e2>"

            offset += 1

        expanded_sentence = Sentence(text, use_tokenizer=False)

        expanded_span_1 = Span([expanded_sentence[span_1_startid - 1]])
        expanded_span_2 = Span([expanded_sentence[span_2_startid - 1]])

        return (
            expanded_sentence,
            (
                expanded_span_1,
                expanded_span_2,
            )
            if entity_one_is_first
            else (expanded_span_2, expanded_span_1),
        )

    def forward_pass(
        self,
        sentences: Union[List[Sentence], Sentence],
        return_label_candidates: bool = False,
    ):

        empty_label_candidates = []
        entity_pairs = []
        labels = []
        sentences_to_label = []

        for sentence in sentences:

            # super lame: make dictionary to find relation annotations for a given entity pair
            relation_dict = {}
            for label in sentence.get_labels(self.label_type):
                relation_label: RelationLabel = label
                relation_dict[create_position_string(relation_label.head, relation_label.tail)] = relation_label

            # get all entity spans
            span_labels = sentence.get_labels(self.entity_label_type)

            # go through cross product of entities, for each pair concat embeddings
            for span_label in span_labels:
                span_1 = span_label.span

                for span_label_2 in span_labels:
                    span_2 = span_label_2.span

                    if span_1 == span_2:
                        continue

                    # filter entity pairs according to their tags if set
                    if (
                        self.entity_pair_filters is not None
                        and (span_label.value, span_label_2.value) not in self.entity_pair_filters
                    ):
                        continue

                    position_string = create_position_string(span_1, span_2)

                    # get gold label for this relation (if one exists)
                    if position_string in relation_dict:
                        relation_label = relation_dict[position_string]
                        label = relation_label.value

                    # if there is no gold label for this entity pair, set to 'O' (no relation)
                    else:
                        if self.train_on_gold_pairs_only:
                            continue  # skip 'O' labels if training on gold pairs only
                        label = "O"

                    entity_pairs.append((span_1, span_2))

                    labels.append([label])

                    # if predicting, also remember sentences and label candidates
                    if return_label_candidates:
                        candidate_label = RelationLabel(head=span_1, tail=span_2, value=None, score=0.0)
                        empty_label_candidates.append(candidate_label)
                        sentences_to_label.append(span_1[0].sentence)

        # if there's at least one entity pair in the sentence
        if len(entity_pairs) > 0:

            # embed sentences and get embeddings for each entity pair
            self.embeddings.embed(sentences)
            relation_embeddings = []

            # get embeddings
            for entity_pair in entity_pairs:
                span_1 = entity_pair[0]
                span_2 = entity_pair[1]

                if self.pooling_operation == "first_last":
                    embedding = torch.cat(
                        [
                            span_1.tokens[0].get_embedding(),
                            span_1.tokens[-1].get_embedding(),
                            span_2.tokens[0].get_embedding(),
                            span_2.tokens[-1].get_embedding(),
                        ]
                    )
                else:
                    embedding = torch.cat([span_1.tokens[0].get_embedding(), span_2.tokens[0].get_embedding()])

                relation_embeddings.append(embedding)

            # stack and drop out (squeeze and unsqueeze)
            embedded_entity_pairs = torch.stack(relation_embeddings).unsqueeze(1)

            embedded_entity_pairs = self.dropout(embedded_entity_pairs)
            embedded_entity_pairs = self.locked_dropout(embedded_entity_pairs)
            embedded_entity_pairs = self.word_dropout(embedded_entity_pairs)

            embedded_entity_pairs = embedded_entity_pairs.squeeze(1)

        else:
            embedded_entity_pairs = None

        if return_label_candidates:
            return (
                embedded_entity_pairs,
                labels,
                sentences_to_label,
                empty_label_candidates,
            )

        return embedded_entity_pairs, labels

    def _get_state_dict(self):
        model_state = {
            **super()._get_state_dict(),
            "embeddings": self.embeddings,
            "label_dictionary": self.label_dictionary,
            "label_type": self.label_type,
            "entity_label_type": self.entity_label_type,
            "weight_dict": self.weight_dict,
            "pooling_operation": self.pooling_operation,
            "dropout_value": self.dropout_value,
            "locked_dropout_value": self.locked_dropout_value,
            "word_dropout_value": self.word_dropout_value,
            "entity_pair_filters": self.entity_pair_filters,
        }
        return model_state

    @classmethod
    def _init_model_with_state_dict(cls, state, **kwargs):

        return super()._init_model_with_state_dict(
            state,
            embeddings=state["embeddings"],
            label_dictionary=state["label_dictionary"],
            label_type=state["label_type"],
            entity_label_type=state["entity_label_type"],
            loss_weights=state["weight_dict"],
            pooling_operation=state["pooling_operation"],
            dropout_value=state["dropout_value"],
            locked_dropout_value=state["locked_dropout_value"],
            word_dropout_value=state["word_dropout_value"],
            entity_pair_filters=state["entity_pair_filters"],
            **kwargs,
        )

    @property
    def label_type(self):
        return self._label_type

    @staticmethod
    def _fetch_model(model_name) -> str:

        model_map = {}

        hu_path: str = "https://nlp.informatik.hu-berlin.de/resources/models"

        model_map["relations-fast"] = "/".join([hu_path, "relations-fast", "relations-fast.pt"])
        model_map["relations"] = "/".join([hu_path, "relations", "relations.pt"])

        cache_dir = Path("models")
        if model_name in model_map:
            model_name = cached_path(model_map[model_name], cache_dir=cache_dir)

        return model_name


def create_position_string(head: Span, tail: Span) -> str:
    return f"{head.id_text} -> {tail.id_text}"
