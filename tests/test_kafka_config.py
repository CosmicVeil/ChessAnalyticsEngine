import importlib.util
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_pipeline_module(module_name):
    module_path = REPOSITORY_ROOT / "python-pipeline" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KafkaConfigTests(unittest.TestCase):
    def test_defaults_to_local_redpanda(self):
        module = load_pipeline_module("kafka_config")
        self.assertEqual(module.get_kafka_bootstrap_servers({}), "localhost:19092")

    def test_uses_trimmed_environment_value(self):
        module = load_pipeline_module("kafka_config")
        self.assertEqual(
            module.get_kafka_bootstrap_servers(
                {"KAFKA_BOOTSTRAP_SERVERS": " broker-a:9092,broker-b:9092 "}
            ),
            "broker-a:9092,broker-b:9092",
        )

    def test_blank_value_uses_default(self):
        module = load_pipeline_module("kafka_config")
        self.assertEqual(
            module.get_kafka_bootstrap_servers({"KAFKA_BOOTSTRAP_SERVERS": "  "}),
            "localhost:19092",
        )


if __name__ == "__main__":
    unittest.main()
