from pathlib import Path

import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

from training_data import get_game_ids, group_train_test_indices, prepare_training_data


def main() -> None:

    device = torch.device("mps" if torch.mps.is_available() else "cpu")
    print(device)

    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    features, labels = prepare_training_data(dataframe)
    game_ids = get_game_ids(dataframe)
    train_index, test_index = group_train_test_indices(features, labels, game_ids)

    X = torch.tensor(features.values, dtype=torch.float32)
    y = torch.tensor(labels.values, dtype=torch.float32).reshape(-1, 1)
    X_train, X_test = X[train_index], X[test_index]
    y_train, y_test = y[train_index], y[test_index]
    dataloader = DataLoader(list(zip(X_train, y_train)), shuffle=True, batch_size=64)

    model = nn.Sequential(
        nn.Linear(X_train.shape[1], 60),
        nn.ReLU(),
        nn.Linear(60, 30),
        nn.ReLU(),
        nn.Linear(30, 1),
        nn.Sigmoid(),
    ).to(device)

    n_epochs = 1000
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
    y_pred = model(X_test.to(device))
    accuracy = float((y_pred.round() == y_test.to(device)).float().mean())
    print("PyTorch model accuracy: %.2f%%" % (accuracy * 100))


if __name__ == "__main__":
    main()
