import sys
from pathlib import Path

# Тесты импортируют модули из корня проекта.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
