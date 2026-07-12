import subprocess
import sys
from pathlib import Path


MODEL_SCRIPTS = {
    1: "train_torch.py",
    2: "train_xgboost.py",
}


def launch_models(model_numbers: list[int]) -> None:
    for model_number in model_numbers:
        script_path = Path(__file__).with_name(MODEL_SCRIPTS[model_number])
        subprocess.run([sys.executable, str(script_path)], check=True)


def main() -> None:
    selection = int(input("Which model to run? (1 or 2). If both, input -1: "))
    if selection == -1:
        launch_models([1, 2])
    elif selection in MODEL_SCRIPTS:
        launch_models([selection])
    else:
        raise ValueError("Choose 1, 2, or -1.")


if __name__ == "__main__":
    main()
