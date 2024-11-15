# Adapted from https://github.com/mshumer/gpt-llm-trainer and
#  https://github.com/arielnlee/Platypus/blob/main/finetune.py

import pandas as pd
from sklearn.model_selection import StratifiedKFold, train_test_split
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    TrainerCallback,
    TrainerState,
    TrainerControl,
    BitsAndBytesConfig,
)
from peft import LoraConfig, set_peft_model_state_dict
from trl import SFTTrainer
import argparse
import torch
import os


def parse_arguments():
    parser = argparse.ArgumentParser()

    # Define the arguments
    parser.add_argument("--fold", type=int, default=None)
    parser.add_argument("--fraction", type=float, required=None)
    parser.add_argument("--model_size", type=int, choices=[7, 13, 70], required=True)
    parser.add_argument("--dataset", type=str, required=True)

    # Parse the arguments
    args = parser.parse_args()

    return args


def get_train_test_fold(fold, dataset, num_splits=10):
    assert fold < num_splits

    system_context = (
        "You are a helpful and unbiased news verification assistant. You will be provided with the"
        " title and the full body of text of a news article. Then, you will answer further questions related"
        " to the given article. Ensure that your answers are grounded in reality,"
        " truthful and reliable."
    )
    prompt = "### Instruction:\n{system_context}\n\n### Input:\n{text}\n\n### Response:\n{label}"
    SEED = 42

    dataset_path = f"data/signals/{dataset}.csv"
    df = pd.read_csv(dataset_path)
    df["prompt"] = df.apply(
        lambda x: prompt.format(
            text=(x["text"]).strip(), system_context=system_context, label="Yes" if x["objective_true"] == 1 else "No"
        ),
        axis=1,
    )
    df = df[["text", "prompt", "objective_true"]]
    skf = StratifiedKFold(n_splits=10, shuffle=True, random_state=SEED)
    for j, (train_idxs, test_idxs) in enumerate(skf.split(range(len(df)), y=df["objective_true"].to_numpy())):
        train_df, test_df = df.iloc[train_idxs], df.iloc[test_idxs]

        if fold == j:
            return train_df, test_df


def get_datasets_fraction(dataset, frac):
    assert frac <= 1.0

    system_context = (
        "You are a helpful and unbiased news verification assistant. You will be provided with the"
        " title and the full body of text of a news article. Then, you will answer further questions related"
        " to the given article. Ensure that your answers are grounded in reality,"
        " truthful and reliable."
    )
    prompt = "### Instruction:\n{system_context}\n\n### Input:\n{text}\n\n### Response:\n{label}"
    SEED = 42

    dataset_path = f"data/signals/{dataset}.csv"
    df = pd.read_csv(dataset_path)
    df["prompt"] = df.apply(
        lambda x: prompt.format(
            text=(x["text"]).strip(), system_context=system_context, label="Yes" if x["objective_true"] == 1 else "No"
        ),
        axis=1,
    )
    df = df[["text", "prompt", "objective_true"]]
    df_train, df_test = train_test_split(df, train_size=0.8, random_state=SEED)
    df_train = df_train.sample(frac=frac, random_state=SEED)  # Get a fraction of the training set

    return df_train, df_test


class SavePeftModelCallback(TrainerCallback):
    def on_save(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        **kwargs,
    ):
        checkpoint_folder = os.path.join(args.output_dir, f"{state.global_step}")

        kwargs["model"].save_pretrained(checkpoint_folder)

        pytorch_model_path = os.path.join(checkpoint_folder, "pytorch_model.bin")
        torch.save({}, pytorch_model_path)
        return control


class LoadBestPeftModelCallback(TrainerCallback):
    def on_train_end(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        **kwargs,
    ):
        print(f"Loading best peft model from {state.best_model_checkpoint} (score: {state.best_metric}).")
        best_model_path = os.path.join(state.best_model_checkpoint, "adapter_model.bin")
        adapters_weights = torch.load(best_model_path)
        model = kwargs["model"]
        set_peft_model_state_dict(model, adapters_weights)
        return control


# %%
if __name__ == "__main__":
    args = parse_arguments()
    DATASET = args.dataset
    FOLD = args.fold
    MODEL_SIZE = args.model_size
    FRACTION = args.fraction

    model_name = f"garage-bAInd/Platypus2-{MODEL_SIZE}B"
    lora_r = 8
    lora_alpha = 16
    lora_dropout = 0.05
    use_4bit = True
    bnb_4bit_compute_dtype = "float16"
    bnb_4bit_quant_type = "nf4"
    use_nested_quant = False
    output_dir = f"results-{DATASET}-{FOLD}"
    num_train_epochs = 1
    fp16 = False
    bf16 = False
    per_device_train_batch_size = 4
    per_device_eval_batch_size = 4
    gradient_accumulation_steps = 1
    gradient_checkpointing = True
    max_grad_norm = 0.3
    learning_rate = 3e-4
    weight_decay = 0.000
    optim = "paged_adamw_32bit"
    lr_scheduler_type = "cosine"
    max_steps = -1
    warmup_ratio = 0.03
    group_by_length = True
    save_steps = 5
    logging_steps = 5
    max_seq_length = None
    packing = False
    device_map = {"": 0}
    if FOLD is not None:
        train_df, test_df = get_train_test_fold(FOLD, DATASET)
    else:
        train_df, test_df = get_datasets_fraction(DATASET, FRACTION)

    # Load datasets
    train_dataset = Dataset.from_pandas(train_df)
    valid_dataset = Dataset.from_pandas(test_df)

    # compute_dtype = getattr(torch, bnb_4bit_compute_dtype)
    compute_dtype = getattr(torch, bnb_4bit_compute_dtype)
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=use_4bit,
        bnb_4bit_quant_type=bnb_4bit_quant_type,
        bnb_4bit_compute_dtype=compute_dtype,
        bnb_4bit_use_double_quant=use_nested_quant,
    )

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        # load_in_8bit=True,
        device_map=device_map,
    )

    model.config.use_cache = False
    model.config.pretraining_tp = 1
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    peft_config = LoraConfig(
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        r=lora_r,
        bias="none",
        task_type="CAUSAL_LM",
    )
    # Set training parameters
    training_arguments = TrainingArguments(
        output_dir=output_dir,
        num_train_epochs=num_train_epochs,
        per_device_train_batch_size=per_device_train_batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        optim=optim,
        save_steps=save_steps,
        load_best_model_at_end=False,
        logging_steps=logging_steps,
        learning_rate=learning_rate,
        weight_decay=weight_decay,
        fp16=fp16,
        bf16=bf16,
        max_grad_norm=max_grad_norm,
        max_steps=max_steps,
        warmup_ratio=warmup_ratio,
        group_by_length=group_by_length,
        lr_scheduler_type=lr_scheduler_type,
        report_to="all",
        evaluation_strategy="steps",
        eval_steps=3000,
        save_total_limit=1,
    )
    # Set supervised fine-tuning parameters
    trainer = SFTTrainer(
        model=model,
        train_dataset=train_dataset,
        eval_dataset=valid_dataset,  # Pass validation dataset here
        peft_config=peft_config,
        dataset_text_field="prompt",
        max_seq_length=max_seq_length,
        tokenizer=tokenizer,
        args=training_arguments,
        packing=packing,
    )

    if os.path.exists(output_dir) and any(fname.startswith("checkpoint") for fname in os.listdir(output_dir)):
        resume_from_checkpoint = True
    else:
        resume_from_checkpoint = False

    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
