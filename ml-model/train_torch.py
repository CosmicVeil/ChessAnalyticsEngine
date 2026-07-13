from pathlib import Path

import pandas as pd
import torch
import torch.optim as optim
from torch.utils.data import DataLoader

from model_artifacts import build_pytorch_model, model_artifact_path, save_feature_schema
from training_data import group_train_test_indices, prepare_game_training_data
import torch.nn as nn

def main() -> None:

    device = torch.device("mps" if torch.mps.is_available() else "cpu")
    print(device)

    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    # Combine every game's moves before training so one prediction represents one whole game.
    features, labels = prepare_game_training_data(dataframe)
    game_ids = pd.Series(features.index, index=features.index, dtype="string")
    train_index, test_index = group_train_test_indices(features, labels, game_ids)

    X = torch.tensor(features.values, dtype=torch.float32)
    y = torch.tensor(labels.values, dtype=torch.float32).reshape(-1, 1)
    X_train, X_test = X[train_index], X[test_index]
    y_train, y_test = y[train_index], y[test_index]
    dataloader = DataLoader(list(zip(X_train, y_train)), shuffle=True, batch_size=64)

    model = build_pytorch_model(X_train.shape[1]).to(device)

    n_epochs = 100
    loss_fn = nn.BCELoss()
    optimizer = optim.SGD(model.parameters(), lr=0.01)
    model.train()
    print("Starting PyTorch model training...")
    for epoch in range(n_epochs):
        total_loss = 0.0
        batch_count = 0

        for X_batch, y_batch in dataloader:

            X_batch = X_batch.to(device)
            y_batch = y_batch.to(device)
            y_pred = model(X_batch)
            loss = loss_fn(y_pred, y_batch)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
            batch_count += 1

        if epoch %5 == 0:
            print(f"Epoch {epoch + 1}/{n_epochs} - loss: {total_loss / batch_count:.4f}")

    model.eval()
    with torch.no_grad():
        y_pred = model(X_test.to(device))
    accuracy = float((y_pred.round() == y_test.to(device)).float().mean())
    print("PyTorch model accuracy: %.2f%%" % (accuracy * 100))

    save_feature_schema(features.columns.tolist())
    model_cpu = model.to("cpu")
    pytorch_path = model_artifact_path("pytorch_model.pt")
    # Save the PyTorch weights and input size for the live inference consumer.
    torch.save({"state_dict": model_cpu.state_dict(), "input_size": X_train.shape[1]}, pytorch_path)
    print(f"Saved PyTorch model to {pytorch_path}")

    onnx_path = model_artifact_path("pytorch_model.onnx")
    try:
        # Export a portable ONNX copy using one real-shaped example tensor.
        torch.onnx.export(
            model_cpu,
            torch.zeros(1, X_train.shape[1]),
            onnx_path,
            export_params=True,
            opset_version=17,
            input_names=["input"],
            output_names=["output"],
        )
        print(f"Saved PyTorch ONNX model to {onnx_path}")
    except (ImportError, ModuleNotFoundError, RuntimeError) as error:
        print(f"Skipped ONNX export: {error}")


if __name__ == "__main__":
    main()
