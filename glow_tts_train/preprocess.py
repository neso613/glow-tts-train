#!/usr/bin/env python3
import argparse
import logging
import json
import csv
import math
import random
import sys
import typing
from pathlib import Path

import gruut
import doit
import phonemes2ids
import torch

from glow_tts_train.config import TrainingConfig, Aligner, MetadataFormat, DatasetConfig
from kaldi_align import KaldiAligner, Utterance

_CONFIG = TrainingConfig()
_OUTPUT_DIR = Path.cwd()

_DELIMITER = "|"

_LOGGER = logging.getLogger("preprocess")

# -----------------------------------------------------------------------------


def make_split(
    input_csv_path: typing.Union[str, Path], val_split: float, val_random: bool, targets
):
    train_path = Path(targets[0])
    train_path.parent.mkdir(parents=True, exist_ok=True)

    val_path = Path(targets[1])
    val_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []

    with open(input_csv_path, "r", encoding="utf-8") as input_file:
        reader = csv.reader(input_file, delimiter=_DELIMITER)
        for row in reader:
            rows.append(row)

    num_val = int(math.ceil(val_split * len(rows)))
    assert num_val > 0, f"No validation rows will be written: {val_path}"

    num_train = len(rows) - num_val
    assert num_train > 0, f"No training rows will be written: {train_path}"

    if val_random:
        random.shuffle(rows)

    with open(train_path, "w", encoding="utf-8") as train_file, open(
        val_path, "w", encoding="utf-8"
    ) as val_file:
        train_writer = csv.writer(train_file, delimiter=_DELIMITER)
        val_writer = csv.writer(val_file, delimiter=_DELIMITER)

        for row_idx, row in enumerate(rows):
            if row_idx < num_train:
                train_writer.writerow(row)
            else:
                val_writer.writerow(row)


def task_split():
    metadata_format = _CONFIG.dataset_format.value

    for dataset in _CONFIG.datasets:
        if dataset.metadata_path is None:
            # Pre-split by user
            continue

        dataset_dir = _OUTPUT_DIR / dataset.name
        metadata_path = Path(dataset.metadata_path)
        if not metadata_path.is_absolute():
            # Interpret as relative to output directory
            metadata_path = _OUTPUT_DIR / str(metadata_path)

        train_path = dataset_dir / f"train_{metadata_format}.csv"
        val_path = dataset_dir / f"val_{metadata_format}.csv"

        yield {
            "name": dataset.name,
            "actions": [
                (make_split, [metadata_path, dataset.val_split, dataset.val_random],)
            ],
            "file_dep": [metadata_path],
            "targets": [train_path, val_path],
        }


# -----------------------------------------------------------------------------


def make_phoneme_ids(
    input_csv_path: typing.Union[str, Path],
    phoneme_map_path: typing.Union[str, Path],
    targets,
):
    target_path = Path(targets[0])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with open(phoneme_map_path, "r", encoding="utf-8") as map_file:
        phoneme_to_id = phonemes2ids.load_phoneme_ids(map_file)

    with open(input_csv_path, "r", encoding="utf-8") as input_file, open(
        target_path, "w", encoding="utf-8"
    ) as output_file:
        reader = csv.reader(input_file, delimiter=_DELIMITER)
        writer = csv.writer(output_file, delimiter=_DELIMITER)

        for row in reader:
            phonemes_str = row[-1]
            word_phonemes = _CONFIG.phonemes.split_word_phonemes(phonemes_str)

            phoneme_ids = phonemes2ids.phonemes2ids(
                word_phonemes=word_phonemes,
                phoneme_to_id=phoneme_to_id,
                pad=_CONFIG.phonemes.pad,
                bos=_CONFIG.phonemes.bos,
                eos=_CONFIG.phonemes.eos,
                auto_bos_eos=_CONFIG.phonemes.auto_bos_eos,
                blank=_CONFIG.phonemes.blank,
                blank_word=_CONFIG.phonemes.blank_word,
                blank_between=_CONFIG.phonemes.blank_between,
                blank_at_start=_CONFIG.phonemes.blank_at_start,
                blank_at_end=_CONFIG.phonemes.blank_at_end,
                simple_punctuation=_CONFIG.phonemes.simple_punctuation,
                punctuation_map=_CONFIG.phonemes.punctuation_map,
                separate=_CONFIG.phonemes.separate,
                separate_graphemes=_CONFIG.phonemes.separate_graphemes,
                separate_tones=_CONFIG.phonemes.separate_tones,
                tone_before=_CONFIG.phonemes.tone_before,
                phoneme_map=_CONFIG.phonemes.phoneme_map,
                fail_on_missing=True,
            )

            phoneme_ids_str = " ".join(str(p_id) for p_id in phoneme_ids)
            writer.writerow((*row, phoneme_ids_str))


def make_phonemes(
    input_csv_path: typing.Union[str, Path], gruut_language: str, targets,
):
    target_path = Path(targets[0])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with open(input_csv_path, "r", encoding="utf-8") as input_file, open(
        target_path, "w", encoding="utf-8"
    ) as output_file:
        reader = csv.reader(input_file, delimiter=_DELIMITER)
        writer = csv.writer(output_file, delimiter=_DELIMITER)

        for row in reader:
            raw_text = row[-1]
            word_phonemes = []

            for sentence in gruut.sentences(raw_text, lang=gruut_language):
                for word in sentence:
                    if word.phonemes:
                        word_phonemes.append(word.phonemes)

            if not word_phonemes:
                _LOGGER.warning("No phonemes for %s", row)
                continue

            phonemes_str = _CONFIG.phonemes.join_word_phonemes(word_phonemes)

            writer.writerow((*row, phonemes_str))


def task_text_to_ids():
    if _CONFIG.dataset_format == MetadataFormat.PHONEME_IDS:
        # Ids should already exist
        return

    phoneme_map_path = _OUTPUT_DIR / "phonemes.txt"

    for dataset in _CONFIG.datasets:

        dataset_language = dataset.text_language or _CONFIG.text_language
        assert (
            dataset_language
        ), f"Need dataset text language for spoken text ({dataset})"

        dataset_dir = _OUTPUT_DIR / dataset.name

        phonemes_format = MetadataFormat.PHONEMES.value
        train_phonemes_path = dataset_dir / f"train_{phonemes_format}.csv"
        val_phonemes_path = dataset_dir / f"val_{phonemes_format}.csv"

        if (_CONFIG.dataset_format == MetadataFormat.TEXT) and (
            _CONFIG.text_aligner != Aligner.KALDI_ALIGN
        ):
            text_format = MetadataFormat.TEXT.value
            train_text_path = dataset_dir / f"train_{text_format}.csv"
            val_text_path = dataset_dir / f"val_{text_format}.csv"

            yield {
                "name": str(train_text_path.relative_to(_OUTPUT_DIR)),
                "actions": [(make_phonemes, [train_text_path, dataset_language])],
                "file_dep": [train_text_path],
                "targets": [train_phonemes_path],
            }

            yield {
                "name": str(val_text_path.relative_to(_OUTPUT_DIR)),
                "actions": [(make_phonemes, [val_text_path, dataset_language])],
                "file_dep": [val_text_path],
                "targets": [val_phonemes_path],
            }

        ids_format = MetadataFormat.PHONEME_IDS.value
        train_ids_path = dataset_dir / f"train_{ids_format}.csv"
        val_ids_path = dataset_dir / f"val_{ids_format}.csv"

        yield {
            "name": str(train_ids_path.relative_to(_OUTPUT_DIR)),
            "actions": [(make_phoneme_ids, [train_phonemes_path, phoneme_map_path])],
            "file_dep": [train_phonemes_path, phoneme_map_path],
            "targets": [train_ids_path],
        }

        yield {
            "name": str(val_ids_path.relative_to(_OUTPUT_DIR)),
            "actions": [(make_phoneme_ids, [val_phonemes_path, phoneme_map_path])],
            "file_dep": [val_phonemes_path, phoneme_map_path],
            "targets": [val_ids_path],
        }


# -----------------------------------------------------------------------------


def make_phoneme_map(
    phoneme_csv_paths: typing.Iterable[typing.Union[str, Path]], targets
):
    target_path = Path(targets[0])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    word_phonemes: typing.List[typing.List[str]] = []

    for input_csv_path in phoneme_csv_paths:
        with open(input_csv_path, "r", encoding="utf-8") as input_file:
            reader = csv.reader(input_file, delimiter=_DELIMITER)
            for row in reader:
                phonemes_str = row[-1]
                word_phonemes.extend(_CONFIG.phonemes.split_word_phonemes(phonemes_str))

    all_phonemes: typing.Set[str] = set()
    phoneme_to_id: typing.Dict[str, int] = dict(_CONFIG.phonemes.phoneme_to_id or {})

    if _CONFIG.phonemes.pad and (_CONFIG.phonemes.pad not in phoneme_to_id):
        # Add pad symbol
        phoneme_to_id[_CONFIG.phonemes.pad] = len(phoneme_to_id)

    if _CONFIG.phonemes.bos and (_CONFIG.phonemes.bos not in phoneme_to_id):
        # Add BOS symbol
        phoneme_to_id[_CONFIG.phonemes.bos] = len(phoneme_to_id)

    if _CONFIG.phonemes.eos and (_CONFIG.phonemes.eos not in phoneme_to_id):
        # Add EOS symbol
        phoneme_to_id[_CONFIG.phonemes.eos] = len(phoneme_to_id)

    if _CONFIG.phonemes.minor_break and (
        _CONFIG.phonemes.minor_break not in phoneme_to_id
    ):
        # Add minor break (short pause)
        phoneme_to_id[_CONFIG.phonemes.minor_break] = len(phoneme_to_id)

    if _CONFIG.phonemes.major_break and (
        _CONFIG.phonemes.major_break not in phoneme_to_id
    ):
        # Add major break (long pause)
        phoneme_to_id[_CONFIG.phonemes.major_break] = len(phoneme_to_id)

    if _CONFIG.phonemes.blank and (_CONFIG.phonemes.blank not in phoneme_to_id):
        # Add blank symbol
        phoneme_to_id[_CONFIG.phonemes.blank] = len(phoneme_to_id)

    if _CONFIG.phonemes.blank_word and (
        _CONFIG.phonemes.blank_word not in phoneme_to_id
    ):
        # Add blank symbol
        phoneme_to_id[_CONFIG.phonemes.blank_word] = len(phoneme_to_id)

    if _CONFIG.phonemes.separate:
        # Add stress symbols
        for stress in sorted(_CONFIG.phonemes.separate):
            if stress not in phoneme_to_id:
                phoneme_to_id[stress] = len(phoneme_to_id)

    phonemes2ids.learn_phoneme_ids(
        word_phonemes=word_phonemes,
        all_phonemes=all_phonemes,
        simple_punctuation=_CONFIG.phonemes.simple_punctuation,
        punctuation_map=_CONFIG.phonemes.punctuation_map,
        separate=_CONFIG.phonemes.separate,
        separate_graphemes=_CONFIG.phonemes.separate_graphemes,
        separate_tones=_CONFIG.phonemes.separate_tones,
        phoneme_map=_CONFIG.phonemes.phoneme_map,
    )

    for phoneme in sorted(all_phonemes):
        if phoneme not in phoneme_to_id:
            phoneme_to_id[phoneme] = len(phoneme_to_id)

    # Write phoneme map
    with open(target_path, "w", encoding="utf-8") as output_file:
        for phoneme, phoneme_idx in phoneme_to_id.items():
            print(phoneme_idx, phoneme, file=output_file)


def task_learn_phoneme_map():
    if _CONFIG.dataset_format == MetadataFormat.PHONEME_IDS:
        # Ids should already exist
        return

    phoneme_map_path = _OUTPUT_DIR / "phonemes.txt"
    phonemes_paths = []

    for dataset in _CONFIG.datasets:

        dataset_dir = _OUTPUT_DIR / dataset.name

        phonemes_format = MetadataFormat.PHONEMES.value
        train_phonemes_path = dataset_dir / f"train_{phonemes_format}.csv"
        val_phonemes_path = dataset_dir / f"val_{phonemes_format}.csv"

        phonemes_paths.append(train_phonemes_path)
        phonemes_paths.append(val_phonemes_path)

    yield {
        "name": phoneme_map_path.name,
        "actions": [(make_phoneme_map, [phonemes_paths])],
        "file_dep": phonemes_paths,
        "targets": [phoneme_map_path],
    }


# -----------------------------------------------------------------------------


def make_spoken_text(
    input_csv_path: typing.Union[str, Path], gruut_language: str, targets
):
    target_path = Path(targets[0])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with open(input_csv_path, "r", encoding="utf-8") as input_file, open(
        target_path, "w", encoding="utf-8"
    ) as output_file:
        reader = csv.reader(input_file, delimiter=_DELIMITER)
        writer = csv.writer(output_file, delimiter=_DELIMITER)

        for row in reader:
            raw_text = row[-1]

            spoken_texts = []
            for sentence in gruut.sentences(
                raw_text, lang=gruut_language, pos=False, phonemes=False
            ):
                spoken_texts.append(sentence.text_spoken)

            spoken_text = " ".join(spoken_texts)
            writer.writerow((*row, spoken_text))


def task_spoken_text():
    if _CONFIG.text_aligner is None:
        return

    assert (
        _CONFIG.dataset_format == MetadataFormat.TEXT
    ), "Cannot align datasets that are not in text format"

    text_format = MetadataFormat.TEXT.value

    for dataset in _CONFIG.datasets:
        dataset_language = dataset.text_language or _CONFIG.text_language
        assert (
            dataset_language
        ), f"Need dataset text language for spoken text ({dataset})"

        dataset_dir = _OUTPUT_DIR / dataset.name

        for split in ("train", "val"):
            text_path = dataset_dir / f"{split}_{text_format}.csv"
            spoken_path = dataset_dir / f"{split}_{text_format}_spoken.csv"

            yield {
                "name": str(spoken_path.relative_to(_OUTPUT_DIR)),
                "actions": [(make_spoken_text, [text_path, dataset_language])],
                "file_dep": [text_path],
                "targets": [spoken_path],
            }


# -----------------------------------------------------------------------------


def make_kaldi_align(
    input_csv_path: typing.Union[str, Path],
    dataset_language: str,
    dataset_config: DatasetConfig,
    align_dir: typing.Union[str, Path],
    targets,
):
    assert (
        dataset_config.audio_dir is not None
    ), f"Audio directory is required for alignment: {dataset_config}"
    audio_dir = Path(dataset_config.audio_dir)

    if not audio_dir.is_absolute():
        audio_dir = _OUTPUT_DIR / str(audio_dir)

    align_dir = Path(align_dir)
    aligner = KaldiAligner(language=dataset_language, output_dir=align_dir)

    align_dir = Path(align_dir)
    align_dir.mkdir(parents=True, exist_ok=True)

    target_path = Path(targets[0])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    utterances = []

    with open(input_csv_path, "r", encoding="utf-8") as input_file:
        reader = csv.reader(input_file, delimiter=_DELIMITER)

        for row in reader:
            utt_id, spoken_text = row[0], row[-1]

            audio_path = audio_dir / utt_id
            if not audio_path.is_file():
                audio_path = audio_dir / f"{utt_id}.wav"

            if not audio_path.is_file():
                _LOGGER.warning("Missing audio file: %s", audio_path)
                continue

            if dataset_config.multispeaker:
                speaker = row[1]
            else:
                speaker = dataset_config.name

            utterances.append(
                Utterance(
                    id=utt_id, speaker=speaker, text=spoken_text, audio_path=audio_path
                )
            )

    aligned_utterances = aligner.align(utterances)

    with open(target_path, "w", encoding="utf-8") as output_file:
        for aligned_utt in aligned_utterances:
            json_line = json.dumps(aligned_utt.to_dict(), ensure_ascii=False)
            print(json_line, file=output_file)


def make_kaldi_align_csv(input_jsonl_path: typing.Union[str, Path], targets):
    target_path = Path(targets[0])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    min_sec = 0.5
    buffer_sec = 0.15
    skip_phones = {"SIL", "SPN", "NSN"}

    with open(input_jsonl_path, "r", encoding="utf-8") as input_file, open(
        target_path, "w", encoding="utf-8"
    ) as output_file:
        writer = csv.writer(output_file, delimiter=_DELIMITER)

        for line in input_file:
            line = line.strip()
            if not line:
                continue

            align_obj = json.loads(line)
            utt_id = align_obj["id"]

            # Find sentence boundaries (exclude <eps> before and after)
            start_sec = -1.0
            end_sec = -1.0

            all_word_phonemes: typing.List[typing.List[str]] = []

            for word in align_obj["words"]:
                word_phonemes = [
                    phone["text"]
                    for phone in word["phones"]
                    if phone["text"] not in skip_phones
                ]
                if word_phonemes:
                    all_word_phonemes.append(word_phonemes)

                if word["text"] != "<eps>":
                    if start_sec < 0:
                        start_sec = word["phones"][0]["start_sec"]
                    else:
                        end_sec = (
                            word["phones"][-1]["start_sec"]
                            + word["phones"][-1]["duration_sec"]
                        )

            # Determine sentence audio duration
            start_sec = max(0, start_sec - buffer_sec)
            end_sec = end_sec + buffer_sec
            if start_sec > end_sec:
                _LOGGER.warning("start > end: %s", align_obj)
                continue

            if (end_sec - start_sec) < min_sec:
                _LOGGER.warning("Trimmed audio < %s: %s", min_sec, align_obj)
                continue

            start_ms = int(start_sec * 1000)
            end_ms = int(end_sec * 1000)
            phonemes_str = _CONFIG.phonemes.join_word_phonemes(all_word_phonemes)

            writer.writerow((utt_id, start_ms, end_ms, phonemes_str))


def task_kaldi_align():
    if _CONFIG.text_aligner != Aligner.KALDI_ALIGN:
        return

    text_format = MetadataFormat.TEXT.value

    for dataset in _CONFIG.datasets:
        dataset_language = dataset.text_language or _CONFIG.text_language
        assert dataset_language, f"Need dataset text language for alignment ({dataset})"

        assert (
            dataset.audio_dir is not None
        ), f"Audio directory is required for alignment {dataset}"

        dataset_dir = _OUTPUT_DIR / dataset.name

        for split in ("train", "val"):
            spoken_path = dataset_dir / f"{split}_{text_format}_spoken.csv"
            align_jsonl_path = dataset_dir / f"{split}_{text_format}_align.jsonl"
            align_csv_path = dataset_dir / f"{split}_{text_format}_align.csv"
            align_output_dir = dataset_dir / f"{split}_{text_format}_align"

            yield {
                "name": str(align_jsonl_path.relative_to(_OUTPUT_DIR)),
                "actions": [
                    (
                        make_kaldi_align,
                        [spoken_path, dataset_language, dataset, align_output_dir],
                    )
                ],
                "file_dep": [spoken_path],
                "targets": [align_jsonl_path],
            }

            yield {
                "name": str(align_csv_path.relative_to(_OUTPUT_DIR)),
                "actions": [(make_kaldi_align_csv, [align_jsonl_path])],
                "file_dep": [align_jsonl_path],
                "targets": [align_csv_path],
            }


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="Path to JSON configuration file")
    parser.add_argument("--output-dir", help="Path to output directory")
    args, rest_args = parser.parse_known_args()

    logging.basicConfig(level=logging.INFO)

    if args.config:
        args.config = Path(args.config)

        with open(args.config, "r", encoding="utf-8") as config_file:
            _CONFIG = TrainingConfig.load(config_file)

        # Default to directory of config file
        _OUTPUT_DIR = args.config.parent

    if args.output_dir:
        _OUTPUT_DIR = Path(args.output_dir)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    random.seed(_CONFIG.seed)

    sys.argv[1:] = rest_args

    doit.run(globals())
