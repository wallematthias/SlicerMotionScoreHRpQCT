from pathlib import Path

import slicer


def main():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "MotionScoreHRpQCT"

    settings = slicer.app.revisionUserSettings()
    key = "Modules/AdditionalPaths"
    current = list(settings.value(key) or [])

    for path in (str(repo_root), str(module_path)):
        if path not in current:
            current.append(path)

    settings.setValue(key, tuple(current))
    print("Updated Modules/AdditionalPaths:")
    for path in current:
        print(f"  - {path}")


if __name__ == "__main__":
    main()
