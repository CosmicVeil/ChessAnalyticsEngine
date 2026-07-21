from pathlib import Path

import pandas as pd
import torch
import torch.optim as optim
from torch.utils.data import DataLoader

from model_artifacts import build_pytorch_model, model_artifact_path, save_feature_schema
from training_data import group_train_test_indices, prepare_game_training_data
import torch.nn as nn
import torch.nn.functional as F
from sklearn.metrics import confusion_matrix

def main() -> None:

    device = torch.device("mps" if torch.mps.is_available() else "cpu")
    print(device)

    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    # Combine every game's moves before training so one prediction represents one whole game.
    features, labels = prepare_game_training_data(dataframe)
    game_ids = pd.Series(features.index, index=features.index, dtype="string")
    train_index, test_index = group_train_test_indices(features, labels, game_ids)


    X = torch.tensor(features.values, dtype=torch.float32)
    X = F.normalize(X, p=2.0, dim=1)
    y = torch.tensor(labels.values, dtype=torch.float32).reshape(-1, 1)
    X_train, X_test = X[train_index], X[test_index]
    y_train, y_test = y[train_index], y[test_index]
    dataloader = DataLoader(list(zip(X_train, y_train)), shuffle=True, batch_size=64)

    model = build_pytorch_model(X_train.shape[1]).to(device)
    label_counts = labels.value_counts().sort_index()

    n_epochs = 200

    num_negatives = label_counts.get(0.0,0)
    num_positives = label_counts.get(1.0,0)

    pos_weight = torch.tensor([num_negatives / num_positives], dtype=torch.float)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight.to(device))

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
        y_pred = torch.sigmoid(model(X_test.to(device)))
    accuracy = float((y_pred.round() == y_test.to(device)).float().mean())
    print("PyTorch model accuracy: %.2f%%" % (accuracy * 100))

    save_feature_schema(features.columns.tolist())
    model_cpu = model.to("cpu")
    pytorch_path = model_artifact_path("pytorch_model.pt")
    # Save the PyTorch weights and input size for the live inference consumer.
    torch.save({"state_dict": model_cpu.state_dict(), "input_size": X_train.shape[1]}, pytorch_path)

    tn, fp, fn, tp = confusion_matrix(y_test.squeeze().to("cpu"), y_pred.squeeze().round().to("cpu")).ravel()

    false_positive_rate = fp / (fp + tn) if fp + tn else 0.0

    print(f"Saved PyTorch model to {pytorch_path}")

    print(f"False Positive rate: {false_positive_rate}")


if __name__ == "__main__":
    main()
