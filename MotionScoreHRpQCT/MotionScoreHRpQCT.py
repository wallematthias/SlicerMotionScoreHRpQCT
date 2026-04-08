import base64
import binascii
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import uuid
from urllib import error as urllib_error
from urllib import request as urllib_request
from pathlib import Path

import ctk
import qt
import slicer
import vtk

from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleWidget,
    ScriptedLoadableModuleLogic,
)


MODULE_VERSION = "0.1.0"
DEFAULT_LICENSE_API = "https://motionscore-license-api.matthias-walle.workers.dev"
LICENSE_HTTP_USER_AGENT = "MotionScoreSlicer/0.1 (+3D-Slicer; Python urllib)"


class MotionScoreHRpQCT(ScriptedLoadableModule):
    def __init__(self, parent):
        super().__init__(parent)
        parent.title = "MotionScoreHRpQCT"
        parent.categories = ["Quantification"]
        parent.dependencies = []
        parent.contributors = ["Matthias Walle"]
        parent.helpText = (
            "GUI wrapper for motionscore core CLI.\n"
            f"Module version: {MODULE_VERSION}"
        )
        parent.acknowledgementText = "Built for streamlined HR-pQCT motion grading workflows."


class MotionScoreHRpQCTLogic(ScriptedLoadableModuleLogic):
    def __init__(self):
        super().__init__()
        self._proc = None
        self._user_terminated = False

    def is_running(self):
        return self._proc is not None

    def run_cli(self, args, on_output=None, on_finished=None):
        if self._proc is not None:
            raise RuntimeError("A motionscore process is already running")
        self._user_terminated = False

        proc = qt.QProcess()
        proc.setProcessChannelMode(qt.QProcess.MergedChannels)

        env = qt.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        # Avoid Slicer ITK ImageIO plugin autoloading noise in subprocess logs.
        if env.contains("ITK_AUTOLOAD_PATH"):
            env.remove("ITK_AUTOLOAD_PATH")
        if env.contains("SITK_AUTOLOAD_PATH"):
            env.remove("SITK_AUTOLOAD_PATH")
        env.insert("ITK_AUTOLOAD_PATH", "")
        env.insert("SITK_AUTOLOAD_PATH", "")

        proc.setProcessEnvironment(env)

        def _read_output():
            raw = proc.readAll()
            try:
                data = bytes(raw)
            except Exception:
                try:
                    data = raw.data()
                    if isinstance(data, str):
                        data = data.encode("utf-8", errors="replace")
                    else:
                        data = bytes(data)
                except Exception:
                    data = str(raw).encode("utf-8", errors="replace")
            text = data.decode("utf-8", errors="replace")
            if on_output and text:
                filtered_lines = []
                for line in text.splitlines(keepends=True):
                    if "MRMLIDImageIO" in line:
                        continue
                    if "ImageIO factory did not return an ImageIOBase" in line:
                        continue
                    filtered_lines.append(line)
                filtered = "".join(filtered_lines)
                if filtered:
                    on_output(filtered)

        def _finished(*signal_args):
            interrupted = bool(self._user_terminated)
            self._user_terminated = False
            self._proc = None
            exit_code = int(signal_args[0]) if len(signal_args) >= 1 else int(proc.exitCode())
            exit_status = signal_args[1] if len(signal_args) >= 2 else proc.exitStatus()
            if on_finished:
                on_finished(exit_code, exit_status, interrupted)

        proc.readyRead.connect(_read_output)
        proc.finished.connect(_finished)

        python_exe = (
            shutil.which("PythonSlicer")
            or (sys.executable if Path(sys.executable).exists() else None)
            or shutil.which("python3")
            or shutil.which("python")
        )
        if python_exe is None:
            raise RuntimeError("Could not find Python executable")

        full_args = ["-m", "motionscore.cli"] + list(args)
        if on_output:
            on_output(f"[process] launching: {python_exe} {' '.join(full_args)}\n")

        proc.start(python_exe, full_args)
        if not proc.waitForStarted(3000):
            raise RuntimeError("Failed to start motionscore process")

        self._proc = proc

    def interrupt(self):
        proc = self._proc
        if proc is None:
            return False
        self._user_terminated = True
        proc.terminate()

        def _force_kill_if_needed():
            if proc.state() != qt.QProcess.NotRunning:
                proc.kill()

        qt.QTimer.singleShot(1500, _force_kill_if_needed)
        return True


class MotionScoreHRpQCTWidget(ScriptedLoadableModuleWidget):
    RUN_SCOPE_ALL = "All scans"
    REVIEW_SCOPE_PENDING = "Pending only"
    REVIEW_SCOPE_ALL = "Low-confidence scans (re-review)"
    CLEAR_ALL_OPERATORS = "All operators"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logic = MotionScoreHRpQCTLogic()
        self._index_rows = {}
        self._review_rows = {}
        self._active_task_name = None
        self._predict_total = 0
        self._predict_done = 0
        self._loaded_scan_id = ""
        self._loaded_volume_node = None
        self._profile_source_pixmap = None
        self._profile_scan_id = ""
        self._slice_observer_node = None
        self._slice_observer_tag = None
        self._all_scan_ids = []
        self._audit_rows = []
        self._auto_loading_scan = False
        self._grading_shortcuts = []
        self._selected_manual_grade = None
        self._grade_history = []

    def setup(self):
        super().setup()

        self.licenseBox = ctk.ctkCollapsibleButton()
        self.licenseBox.text = "License"
        self.licenseBox.collapsed = True
        self.layout.addWidget(self.licenseBox)

        licenseLayout = qt.QVBoxLayout(self.licenseBox)
        licenseForm = qt.QFormLayout()
        licenseLayout.addLayout(licenseForm)

        self.modelsPathEdit = ctk.ctkPathLineEdit()
        self.modelsPathEdit.filters = ctk.ctkPathLineEdit.Dirs
        self.modelsPathEdit.settingKey = "MotionScore/InternalModelsRoot"
        self.modelsPathEdit.currentPath = str(self._default_models_path())
        self.modelsPathEdit.enabled = False

        self.licenseApiEdit = qt.QLineEdit()
        self.licenseApiEdit.setText(self._settings().value("MotionScore/LicenseApiBase", DEFAULT_LICENSE_API))

        self.licenseNameEdit = qt.QLineEdit()
        self.licenseNameEdit.setText(self._settings().value("MotionScore/LicenseName", ""))
        licenseForm.addRow("Name", self.licenseNameEdit)

        self.licenseInstitutionEdit = qt.QLineEdit()
        self.licenseInstitutionEdit.setText(self._settings().value("MotionScore/LicenseInstitution", ""))
        licenseForm.addRow("Institution", self.licenseInstitutionEdit)

        self.licenseEmailEdit = qt.QLineEdit()
        self.licenseEmailEdit.setText(self._settings().value("MotionScore/LicenseEmail", ""))
        licenseForm.addRow("Email", self.licenseEmailEdit)

        self.licenseKeyEdit = qt.QLineEdit()
        self.licenseKeyEdit.setText(self._settings().value("MotionScore/LicenseKey", ""))
        licenseForm.addRow("License Key", self.licenseKeyEdit)

        self.modelVersionEdit = qt.QLineEdit()
        self.modelVersionEdit.setText(self._settings().value("MotionScore/ModelVersion", "v1"))

        self.quickSetupButton = qt.QPushButton("One-Click Setup")
        licenseLayout.addWidget(self.quickSetupButton)

        self.licenseFlowHelpLabel = qt.QLabel(
            "Fill Name/Institution/Email, click One-Click Setup. "
            "It requests key, activates, and downloads models automatically."
        )
        self.licenseFlowHelpLabel.setWordWrap(True)
        licenseLayout.addWidget(self.licenseFlowHelpLabel)

        self.licenseStatusLabel = qt.QLabel("License: not activated")
        self.licenseStatusLabel.setWordWrap(True)
        licenseLayout.addWidget(self.licenseStatusLabel)

        self.runBox = ctk.ctkCollapsibleButton()
        self.runBox.text = "Run"
        self.runBox.collapsed = False
        self.layout.addWidget(self.runBox)

        runLayout = qt.QVBoxLayout(self.runBox)
        runForm = qt.QFormLayout()
        runLayout.addLayout(runForm)

        self.datasetPathEdit = ctk.ctkPathLineEdit()
        self.datasetPathEdit.filters = ctk.ctkPathLineEdit.Dirs
        self.datasetPathEdit.settingKey = "MotionScore/DatasetRoot"
        self.datasetPathEdit.currentPath = ""
        self.datasetPathEdit.setToolTip("Select dataset folder (browse starts at home when empty).")
        runForm.addRow("Dataset Root", self.datasetPathEdit)

        runButtonsRow = qt.QHBoxLayout()
        self.runButton = qt.QPushButton("Run Predict")
        runButtonsRow.addWidget(self.runButton)
        runLayout.addLayout(runButtonsRow)

        self.progressLabel = qt.QLabel("Idle")
        self.progressBar = qt.QProgressBar()
        self.progressBar.minimum = 0
        self.progressBar.maximum = 100
        self.progressBar.value = 0
        self.progressBar.textVisible = True
        runLayout.addWidget(self.progressLabel)
        runLayout.addWidget(self.progressBar)

        # Advanced options (moved out of main Run flow to declutter first-time usage).
        self.confidenceSpin = qt.QSpinBox()
        self.confidenceSpin.minimum = 0
        self.confidenceSpin.maximum = 100
        self.confidenceSpin.value = 75

        self.trainingModeCheck = qt.QCheckBox("Blind operator until manual grade is submitted")
        self.trainingModeCheck.setChecked(False)

        self.runScopeCombo = qt.QComboBox()
        self.runScopeCombo.addItem(self.RUN_SCOPE_ALL)
        self.interruptButton = qt.QPushButton("Interrupt")
        self.interruptButton.enabled = False
        self.refreshButton = qt.QPushButton("Refresh Review")
        self.exportButton = qt.QPushButton("Export Final Grades")

        reviewBox = ctk.ctkCollapsibleButton()
        reviewBox.text = "Review"
        reviewLayout = qt.QFormLayout(reviewBox)

        self.scanCombo = qt.QComboBox()
        reviewLayout.addRow("Selected Scan", self.scanCombo)

        self.reviewScopeCombo = qt.QComboBox()
        self.reviewScopeCombo.addItems([self.REVIEW_SCOPE_PENDING, self.REVIEW_SCOPE_ALL])
        reviewLayout.addRow("Review Scope", self.reviewScopeCombo)

        self.reviewQueueLabel = qt.QLabel("Queue: shown=0 | pending=0 | reviewed=0/0")
        reviewLayout.addRow("Review Queue", self.reviewQueueLabel)

        self.autoLabel = qt.QLabel("Auto grade: - | confidence: -")
        reviewLayout.addRow("Suggestion", self.autoLabel)

        self.agreementLabel = qt.QLabel("Agreement: overlap=0 | match=- | exact=-")
        reviewLayout.addRow("Operator vs AI", self.agreementLabel)

        self.agreementMatrixTable = qt.QTableWidget()
        self.agreementMatrixTable.setMinimumHeight(180)
        self.agreementMatrixTable.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.agreementMatrixTable.setSelectionMode(qt.QAbstractItemView.NoSelection)
        self.agreementMatrixTable.setWordWrap(True)
        self.agreementMatrixTable.setAlternatingRowColors(True)
        self.agreementMatrixTable.setSizeAdjustPolicy(qt.QAbstractScrollArea.AdjustToContents)
        self.agreementMatrixBox = ctk.ctkCollapsibleButton()
        self.agreementMatrixBox.text = "Agreement Matrix"
        self.agreementMatrixBox.collapsed = True
        agreementMatrixLayout = qt.QVBoxLayout(self.agreementMatrixBox)
        agreementMatrixLayout.addWidget(self.agreementMatrixTable)
        reviewLayout.addRow(self.agreementMatrixBox)

        self.trainingRevealLabel = qt.QLabel("Last training reveal: -")
        self.trainingRevealLabel.setWordWrap(True)
        reviewLayout.addRow("Training Reveal", self.trainingRevealLabel)

        self.profileLabel = qt.QLabel("Slice profile plot: -")
        self.profileLabel.setMinimumHeight(140)
        self.profileLabel.setMinimumWidth(300)
        self.profileLabel.setMaximumWidth(800)
        self.profileLabel.setAlignment(qt.Qt.AlignCenter)
        self.profileLabel.setStyleSheet("QLabel { background: #ffffff; color: #333333; border: 1px solid #cfcfcf; }")
        self.sliceProfileBox = ctk.ctkCollapsibleButton()
        self.sliceProfileBox.text = "Slice Profile"
        self.sliceProfileBox.collapsed = False
        sliceProfileLayout = qt.QVBoxLayout(self.sliceProfileBox)
        sliceProfileLayout.addWidget(self.profileLabel)
        reviewLayout.addRow(self.sliceProfileBox)

        self.reviewerEdit = qt.QLineEdit()
        self.reviewerEdit.placeholderText = "reviewer id"
        self.reviewerEdit.setText(str(self._settings().value("MotionScore/Reviewer", "") or ""))
        reviewLayout.addRow("Reviewer", self.reviewerEdit)

        quickGradeRow = qt.QHBoxLayout()
        self.quickGradeButtons = {}
        quick_grade_styles = self._grade_style_map()
        for grade in range(1, 6):
            btn = qt.QPushButton(f"Grade {grade}")
            btn.setCheckable(True)
            btn.setToolTip(f"Save grade {grade} and move to next (shortcut: Ctrl+{grade})")
            palette = quick_grade_styles.get(
                grade,
                {"bg": "#555", "hover": "#4a4a4a", "pressed": "#444", "text": "#fff"},
            )
            btn.setStyleSheet(
                "QPushButton {"
                f" background-color: {palette['bg']};"
                f" color: {palette['text']};"
                " border: 1px solid rgba(0, 0, 0, 0.35);"
                " border-radius: 4px;"
                " padding: 4px 8px;"
                " font-weight: 600;"
                "}"
                f"QPushButton:hover {{ background-color: {palette['hover']}; }}"
                f"QPushButton:pressed {{ background-color: {palette['pressed']}; }}"
                "QPushButton:checked { border: 2px solid #ffffff; padding: 3px 7px; }"
                "QPushButton[suggested=\"true\"] { border: 5px solid #000000; padding: 0px 4px; }"
                "QPushButton:disabled { background-color: #888; color: #ddd; }"
            )
            btn.clicked.connect(lambda _checked=False, g=grade: self.onQuickSelectGrade(g))
            quickGradeRow.addWidget(btn)
            self.quickGradeButtons[grade] = btn
        reviewLayout.addRow("Quick Grade", quickGradeRow)

        self.autoLoadCheck = qt.QCheckBox("Auto-load selected scan")
        self.autoLoadCheck.setChecked(True)
        self.autoLoadCheck.setToolTip("Automatically load/reload the selected scan to reduce clicks.")

        reviewActionRow = qt.QHBoxLayout()
        self.backButton = qt.QPushButton("Back")
        self.backButton.setToolTip("Go back to the last graded scan to overwrite if needed.")
        self.backButton.enabled = False
        self.applyButton = qt.QPushButton("Save Grade + Next")
        self.applyButton.setVisible(False)
        reviewActionRow.addWidget(self.backButton)
        reviewActionRow.addWidget(self.exportButton)
        reviewLayout.addRow(reviewActionRow)

        self.clearReviewerCombo = qt.QComboBox()
        self.clearReviewerCombo.setEditable(True)
        self.clearReviewerCombo.addItem(self.CLEAR_ALL_OPERATORS)
        self.clearButton = qt.QPushButton("Clear Grades")
        self.loadScanButton = qt.QPushButton("Load / Reload Scan")

        self.layout.addWidget(reviewBox)

        self.additionalOptionsBox = ctk.ctkCollapsibleButton()
        self.additionalOptionsBox.text = "Additional Options"
        self.additionalOptionsBox.collapsed = True
        additionalLayout = qt.QVBoxLayout(self.additionalOptionsBox)

        self.setupStatusLabel = qt.QLabel("Install: checking... | License: checking...")
        self.setupStatusLabel.setWordWrap(True)
        additionalLayout.addWidget(self.setupStatusLabel)

        additionalForm = qt.QFormLayout()
        additionalForm.addRow("Confidence Threshold", self.confidenceSpin)
        additionalForm.addRow("Training Mode", self.trainingModeCheck)
        additionalForm.addRow("Run Scope", self.runScopeCombo)
        additionalForm.addRow("Auto Load", self.autoLoadCheck)
        additionalLayout.addLayout(additionalForm)

        additionalRunButtons = qt.QHBoxLayout()
        additionalRunButtons.addWidget(self.interruptButton)
        additionalRunButtons.addWidget(self.refreshButton)
        additionalLayout.addLayout(additionalRunButtons)

        additionalReviewButtons = qt.QHBoxLayout()
        additionalReviewButtons.addWidget(self.loadScanButton)
        additionalLayout.addLayout(additionalReviewButtons)

        clearRow = qt.QHBoxLayout()
        clearRow.addWidget(self.clearReviewerCombo)
        clearRow.addWidget(self.clearButton)
        additionalForm.addRow("Clear Reviewer", clearRow)

        self.logText = qt.QPlainTextEdit()
        self.logText.readOnly = True
        self.logText.setMaximumBlockCount(5000)
        additionalLayout.addWidget(self.logText)

        self.layout.addWidget(self.additionalOptionsBox)

        self.runButton.clicked.connect(self.onRunPredict)
        self.interruptButton.clicked.connect(self.onInterrupt)
        self.refreshButton.clicked.connect(self.onRefreshReview)
        self.exportButton.clicked.connect(self.onExport)
        self.quickSetupButton.clicked.connect(self.onQuickSetup)
        self.backButton.clicked.connect(self.onBackToPreviousScan)
        self.clearButton.clicked.connect(self.onClearGrades)
        self.loadScanButton.clicked.connect(self.onLoadSelectedScan)
        self.scanCombo.currentTextChanged.connect(self.onScanSelectionChanged)
        self.reviewScopeCombo.currentTextChanged.connect(self.onReviewScopeChanged)
        self.confidenceSpin.valueChanged.connect(self.onConfidenceThresholdChanged)
        self.datasetPathEdit.currentPathChanged.connect(self.onDatasetPathChanged)
        self.trainingModeCheck.toggled.connect(self.onTrainingModeToggled)
        self.reviewerEdit.editingFinished.connect(self._persist_reviewer_setting)
        self.licenseApiEdit.editingFinished.connect(self._persist_license_settings)
        self.licenseNameEdit.editingFinished.connect(self._persist_license_settings)
        self.licenseInstitutionEdit.editingFinished.connect(self._persist_license_settings)
        self.licenseEmailEdit.editingFinished.connect(self._persist_license_settings)
        self.licenseKeyEdit.editingFinished.connect(self._persist_license_settings)

        self._install_grading_shortcuts()

        self.layout.addStretch(1)
        self._update_setup_status()
        qt.QTimer.singleShot(0, self._install_slice_observer)

    def _default_models_path(self):
        app_data = str(qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppDataLocation) or "").strip()
        if app_data:
            root = Path(app_data)
        else:
            root = Path.home() / ".motionscore"
        models_root = (root / "MotionScore" / "models").resolve()
        models_root.mkdir(parents=True, exist_ok=True)
        return models_root

    def _settings(self):
        return qt.QSettings()

    def _persist_license_settings(self):
        s = self._settings()
        s.setValue("MotionScore/LicenseApiBase", self.licenseApiEdit.text.strip())
        s.setValue("MotionScore/LicenseName", self.licenseNameEdit.text.strip())
        s.setValue("MotionScore/LicenseInstitution", self.licenseInstitutionEdit.text.strip())
        s.setValue("MotionScore/LicenseEmail", self.licenseEmailEdit.text.strip())
        s.setValue("MotionScore/LicenseKey", self.licenseKeyEdit.text.strip())
        s.setValue("MotionScore/ModelVersion", self.modelVersionEdit.text.strip())

    def _persist_reviewer_setting(self):
        self._settings().setValue("MotionScore/Reviewer", self.reviewerEdit.text.strip())

    def _install_grading_shortcuts(self):
        parent_widget = self.parent if isinstance(self.parent, qt.QWidget) else slicer.util.mainWindow()
        if parent_widget is None:
            return
        self._grading_shortcuts = []
        for grade in range(1, 6):
            shortcut = qt.QShortcut(qt.QKeySequence(f"Ctrl+{grade}"), parent_widget)
            shortcut.setContext(qt.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda g=grade: self.onQuickSelectGrade(g))
            self._grading_shortcuts.append(shortcut)

    def _auto_load_enabled(self):
        checked_attr = self.autoLoadCheck.checked
        return bool(checked_attr() if callable(checked_attr) else checked_attr)

    def _set_selected_manual_grade(self, grade, suggested=False):
        normalized = None
        try:
            value = int(grade)
            if 1 <= value <= 5:
                normalized = value
        except Exception:
            normalized = None
        self._selected_manual_grade = normalized
        for button_grade, button in self.quickGradeButtons.items():
            is_selected = bool(normalized is not None and button_grade == normalized)
            button.setChecked(is_selected)
            button.setProperty("suggested", bool(suggested and is_selected))
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()
        current_scan = self._combo_text(self.scanCombo) if hasattr(self, "scanCombo") else ""
        if current_scan:
            self._render_profile_plot(current_scan)

    def _grade_style_map(self):
        return {
            1: {"bg": "#125FAF", "hover": "#0F518F", "pressed": "#0C4579", "text": "#FFFFFF", "plot": "#125FAF"},
            2: {"bg": "#4B97E0", "hover": "#3F86C9", "pressed": "#366FA8", "text": "#FFFFFF", "plot": "#4B97E0"},
            3: {"bg": "#F2C94C", "hover": "#E2BA45", "pressed": "#C79F39", "text": "#1A1A1A", "plot": "#F2C94C"},
            4: {"bg": "#E56A5D", "hover": "#D25E52", "pressed": "#B85248", "text": "#FFFFFF", "plot": "#E56A5D"},
            5: {"bg": "#B73A31", "hover": "#A4342C", "pressed": "#8E2D26", "text": "#FFFFFF", "plot": "#B73A31"},
        }

    def _grade_plot_color(self, grade):
        style = self._grade_style_map().get(int(grade), self._grade_style_map()[3])
        return qt.QColor(style["plot"])

    def _set_license_status(self, text):
        self.licenseStatusLabel.setText(str(text))
        self._update_setup_status()

    def _license_token(self):
        return str(self._settings().value("MotionScore/LicenseToken", "") or "").strip()

    def _license_decrypt_key(self):
        return str(self._settings().value("MotionScore/ModelDecryptKey", "") or "").strip()

    def _set_license_session(self, token, decrypt_key):
        s = self._settings()
        s.setValue("MotionScore/LicenseToken", str(token).strip())
        s.setValue("MotionScore/ModelDecryptKey", str(decrypt_key).strip())
        self._update_setup_status()

    def _default_device_hash(self):
        machine = platform.machine()
        node = platform.node()
        mac = f"{uuid.getnode():012x}"
        raw = f"{machine}|{node}|{mac}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

    def _license_api_base(self):
        base = self.licenseApiEdit.text.strip() or DEFAULT_LICENSE_API
        base = base.rstrip("/")
        return base

    def _http_json(self, method, url, payload=None, headers=None):
        headers = dict(headers or {})
        headers.setdefault("accept", "application/json")
        headers.setdefault("user-agent", LICENSE_HTTP_USER_AGENT)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        req = urllib_request.Request(url=url, data=body, method=str(method).upper())
        for k, v in headers.items():
            req.add_header(str(k), str(v))
        try:
            with urllib_request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except urllib_error.HTTPError as exc:
            err_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {err_text}") from exc
        except Exception as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Invalid JSON response: {text[:200]}") from exc

    def _log(self, text):
        self.logText.moveCursor(qt.QTextCursor.End)
        self.logText.insertPlainText(text)
        self.logText.moveCursor(qt.QTextCursor.End)

    def _derivatives_root(self):
        dataset = self.datasetPathEdit.currentPath.strip()
        if not dataset:
            return None
        return Path(dataset) / "MotionScore"

    def _models_dir(self):
        path = str(self.modelsPathEdit.currentPath).strip()
        if not path:
            path = str(self._default_models_path())
            self.modelsPathEdit.currentPath = path
        models_dir = Path(path).resolve()
        models_dir.mkdir(parents=True, exist_ok=True)
        return models_dir

    def _has_local_models(self, models_dir=None):
        models_dir = models_dir or self._models_dir()
        if models_dir is None:
            return False
        return any(models_dir.glob("DNN_*.pt")) or any(models_dir.glob("DNN_*.h5"))

    def _license_ready(self):
        return bool(self._license_token() and self._license_decrypt_key())

    def _update_setup_status(self):
        if not hasattr(self, "setupStatusLabel"):
            return
        install_txt = "ready" if self._has_local_models() else "missing"
        license_txt = "active" if self._license_ready() else "activate"
        self.setupStatusLabel.setText(f"Install: {install_txt} | License: {license_txt}")

    def _training_mode_enabled(self):
        checked_attr = self.trainingModeCheck.checked
        return bool(checked_attr() if callable(checked_attr) else checked_attr)

    def _set_buttons_enabled(self, enabled):
        self.runButton.enabled = enabled
        self.refreshButton.enabled = enabled
        self.exportButton.enabled = enabled
        self.loadScanButton.enabled = enabled
        self.applyButton.enabled = enabled
        self.backButton.enabled = bool(enabled and self._grade_history)
        self.clearButton.enabled = enabled
        self.quickSetupButton.enabled = enabled
        self.trainingModeCheck.enabled = enabled
        self.runScopeCombo.enabled = enabled
        self.reviewScopeCombo.enabled = enabled
        self.clearReviewerCombo.enabled = enabled
        self.autoLoadCheck.enabled = enabled
        for btn in self.quickGradeButtons.values():
            btn.enabled = enabled
        self.interruptButton.enabled = not enabled

    def _run_cli(self, args, on_finish=None):
        try:
            self._set_buttons_enabled(False)
            self._active_task_name = args[0] if args else None
            self.logic.run_cli(
                args=args,
                on_output=self._on_process_output,
                on_finished=lambda code, status, interrupted: self._on_process_finished(code, status, on_finish, interrupted),
            )
        except Exception as exc:
            self._set_buttons_enabled(True)
            self._set_progress_idle()
            slicer.util.errorDisplay(str(exc))

    def _on_process_finished(self, code, _status, callback, interrupted=False):
        self._set_buttons_enabled(True)
        self._log(f"[process] finished with exit code {code}\n")
        task_name = self._active_task_name
        self._active_task_name = None
        if interrupted:
            self._log("[process] interrupted by user\n")
            self.progressLabel.setText("Interrupted")
            return
        if code != 0:
            self.progressLabel.setText("Failed")
            slicer.util.errorDisplay(f"motionscore command failed with exit code {code}")
            return
        if task_name == "predict":
            if self._predict_total > 0:
                done = min(self._predict_done, self._predict_total)
                self.progressBar.minimum = 0
                self.progressBar.maximum = self._predict_total
                self.progressBar.value = done
                self.progressLabel.setText(f"Completed {done}/{self._predict_total}")
            else:
                self.progressLabel.setText(f"Completed {self._predict_done} scan(s)")
            try:
                self.runBox.collapsed = True
            except Exception:
                pass
        else:
            self._set_progress_idle()
        if callback is not None:
            callback()

    def onInterrupt(self):
        if self.logic.interrupt():
            self._log("[process] interrupt requested\n")
            self.progressLabel.setText("Interrupt requested...")

    def onQuickSetup(self):
        """
        Friendly setup flow:
        1) Request key (if missing)
        2) Activate (if token missing)
        3) Download models (if not available)
        """
        self._persist_license_settings()
        if self._license_ready() and self._has_local_models():
            self._set_license_status("License already active. Models already installed.")
            self._log("[license] setup skipped: already ready\n")
            return

        if not self.licenseKeyEdit.text.strip():
            if not self.onLicenseSignup():
                return

        if not self._license_ready():
            if not self.onLicenseActivate():
                return

        if not self._has_local_models():
            if not self.onLicenseFetchModels():
                return

        self._set_license_status("One-click setup complete.")
        self._log("[license] setup complete: local models available\n")

    def onRunPredict(self):
        dataset = self.datasetPathEdit.currentPath.strip()
        if not dataset:
            slicer.util.errorDisplay("Please choose Dataset Root")
            return
        models_dir = self._models_dir()
        if models_dir is None or not models_dir.exists():
            slicer.util.errorDisplay("Could not resolve local models folder.")
            return
        has_models = self._has_local_models(models_dir)
        if not has_models:
            self._log("[setup] no local model files found; running one-click setup.\n")
            self.onQuickSetup()
            has_models = self._has_local_models(models_dir)
            if not has_models:
                slicer.util.errorDisplay(
                    "No model files available yet.\n"
                    "Please complete setup (request key, activate, download models), then try again."
                )
                return

        selected_scope = self._combo_text(self.runScopeCombo)
        if selected_scope == self.RUN_SCOPE_ALL and self._all_scans_already_predicted():
            self._log("[predict] all discovered scans already predicted; refreshing review only.\n")
            self.refreshReview()
            self.progressBar.minimum = 0
            self.progressBar.maximum = 100
            self.progressBar.value = 100
            self.progressLabel.setText("Up to date (review refreshed)")
            return

        args = [
            "predict",
            dataset,
            "--confidence-threshold",
            str(int(self.confidenceSpin.value)),
            "--backend",
            "torch",
            "--model-dir",
            str(models_dir),
            "--output-root",
            dataset,
        ]
        if self._training_mode_enabled():
            args.append("--training-mode")
        if selected_scope and selected_scope != self.RUN_SCOPE_ALL:
            args.extend(["--scan-id", selected_scope])

        self._start_predict_progress(selected_scope)
        self._run_cli(args, on_finish=self.refreshReview)

    def onLicenseSignup(self):
        base = self._license_api_base()
        name = self.licenseNameEdit.text.strip()
        institution = self.licenseInstitutionEdit.text.strip()
        email = self.licenseEmailEdit.text.strip()
        if not name or not institution or not email:
            slicer.util.errorDisplay("Please fill Name, Institution, and Email before signup.")
            return False
        self._persist_license_settings()
        try:
            payload = {
                "name": name,
                "institution": institution,
                "email": email,
            }
            res = self._http_json("POST", f"{base}/signup", payload=payload)
            if not bool(res.get("ok", False)):
                raise RuntimeError(str(res.get("error", "signup_failed")))
            license_key = (
                ((res.get("license") or {}).get("license_key"))
                if isinstance(res.get("license"), dict)
                else None
            )
            if license_key:
                self.licenseKeyEdit.setText(str(license_key))
                self._persist_license_settings()
            self._set_license_status(
                f"License signup successful. Key issued for {email}."
            )
            self._log(f"[license] signup ok: {email}\n")
            return True
        except Exception as exc:
            self._set_license_status(f"License signup failed: {exc}")
            slicer.util.errorDisplay(f"Signup failed:\n{exc}")
            return False

    def onLicenseActivate(self):
        base = self._license_api_base()
        email = self.licenseEmailEdit.text.strip()
        license_key = self.licenseKeyEdit.text.strip()
        if not email or not license_key:
            slicer.util.errorDisplay("Please enter Email and License Key before activation.")
            return False
        self._persist_license_settings()
        try:
            payload = {
                "email": email,
                "license_key": license_key,
                "device_hash": self._default_device_hash(),
            }
            res = self._http_json("POST", f"{base}/activate", payload=payload)
            if not bool(res.get("ok", False)):
                raise RuntimeError(str(res.get("error", "activation_failed")))
            token = str(res.get("token", "")).strip()
            decrypt_key = str(res.get("model_decrypt_key", "")).strip()
            if not token:
                raise RuntimeError("Missing token in activation response")
            if not decrypt_key:
                raise RuntimeError("Missing model_decrypt_key in activation response")
            self._set_license_session(token=token, decrypt_key=decrypt_key)
            lic = res.get("license", {})
            expires_txt = ""
            if isinstance(lic, dict):
                expires_txt = str(lic.get("expires_at", "")).strip()
            self._set_license_status(f"License activated. Expires: {expires_txt or 'unknown'}")
            self._log(f"[license] activated: {email}\n")
            return True
        except Exception as exc:
            self._set_license_status(f"License activation failed: {exc}")
            slicer.util.errorDisplay(f"Activation failed:\n{exc}")
            return False

    def _safe_extract_tar_gz_bytes(self, blob, output_dir):
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            members = tf.getmembers()
            for member in members:
                target = (output_dir / member.name).resolve()
                if output_dir not in target.parents and target != output_dir:
                    raise RuntimeError(f"Unsafe path in model bundle: {member.name}")
            tf.extractall(path=output_dir)

    def onLicenseFetchModels(self):
        base = self._license_api_base()
        version = self.modelVersionEdit.text.strip() or "v1"
        token = self._license_token()
        decrypt_key = self._license_decrypt_key()
        if not token or not decrypt_key:
            slicer.util.errorDisplay("Please activate license first (token/decrypt key missing).")
            return False

        models_dir = self._models_dir()
        if models_dir is None:
            slicer.util.errorDisplay("Could not resolve local models folder.")
            return False

        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json",
            "user-agent": LICENSE_HTTP_USER_AGENT,
        }
        try:
            manifest = self._http_json("GET", f"{base}/model/{version}/manifest", headers=headers)
            enc_req = urllib_request.Request(
                url=f"{base}/model/{version}",
                method="GET",
                headers=headers,
            )
            try:
                with urllib_request.urlopen(enc_req, timeout=60) as resp:
                    enc_bytes = resp.read()
            except urllib_error.HTTPError as exc:
                err_text = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {exc.code}: {err_text}") from exc

            expected_enc_sha = str(manifest.get("encrypted_sha256", "")).strip().lower()
            if expected_enc_sha:
                got_enc_sha = hashlib.sha256(enc_bytes).hexdigest().lower()
                if got_enc_sha != expected_enc_sha:
                    raise RuntimeError(
                        f"Encrypted bundle SHA mismatch: expected={expected_enc_sha} got={got_enc_sha}"
                    )

            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except Exception as exc:
                raise RuntimeError(
                    "Missing dependency 'cryptography'. Install in Slicer Python:\n"
                    "/Applications/Slicer.app/Contents/bin/PythonSlicer -m pip install cryptography"
                ) from exc

            try:
                key = base64.b64decode(decrypt_key, validate=True)
            except binascii.Error as exc:
                raise RuntimeError(
                    "Invalid model_decrypt_key format. Expected base64-encoded AES-256 key."
                ) from exc
            if len(key) != 32:
                raise RuntimeError(
                    f"Invalid model_decrypt_key length: expected 32 bytes after base64 decode, got {len(key)}."
                )

            try:
                nonce = base64.b64decode(str(manifest.get("nonce_base64", "")).strip(), validate=True)
                aad = base64.b64decode(str(manifest.get("aad_base64", "")).strip(), validate=True)
            except binascii.Error as exc:
                raise RuntimeError("Invalid manifest encoding for nonce/aad.") from exc
            plain = AESGCM(key).decrypt(nonce, enc_bytes, aad)

            expected_plain_sha = str(manifest.get("plaintext_sha256", "")).strip().lower()
            if expected_plain_sha:
                got_plain_sha = hashlib.sha256(plain).hexdigest().lower()
                if got_plain_sha != expected_plain_sha:
                    raise RuntimeError(
                        f"Plain bundle SHA mismatch: expected={expected_plain_sha} got={got_plain_sha}"
                    )

            self._safe_extract_tar_gz_bytes(plain, models_dir)
            self.modelsPathEdit.currentPath = str(models_dir)
            self._set_license_status(f"Models fetched and decrypted for version {version}.")
            self._log(f"[license] models fetched: version={version} -> {models_dir}\n")
            self._update_setup_status()
            return True
        except Exception as exc:
            err = str(exc)
            if "model_not_found" in err:
                msg = (
                    f"Model fetch failed: version '{version}' is not uploaded to R2 yet.\n\n"
                    f"Expected objects:\n"
                    f"- models/{version}.manifest.json\n"
                    f"- models/{version}.enc\n\n"
                    f"Upload those files, then try Download Models again."
                )
                self._set_license_status(msg)
                slicer.util.errorDisplay(msg)
            else:
                self._set_license_status(f"Model fetch failed: {err}")
                slicer.util.errorDisplay(f"Fetch Models failed:\n{err}")
            self._update_setup_status()
            return False

    def onRefreshReview(self):
        derivatives = self._derivatives_root()
        if derivatives is None:
            slicer.util.errorDisplay("Please choose Dataset Root")
            return
        index_path = derivatives / "index.tsv"
        if not index_path.exists():
            # Nothing predicted yet; just refresh view state.
            self.refreshReview()
            return

        args = [
            "review-init",
            str(derivatives),
            "--confidence-threshold",
            str(int(self.confidenceSpin.value)),
        ]
        if self._training_mode_enabled():
            args.append("--training-mode")
        self._run_cli(args, on_finish=self.refreshReview)

    def refreshReview(self):
        derivatives = self._derivatives_root()
        if derivatives is None:
            slicer.util.errorDisplay("Please choose Dataset Root")
            return

        index_path = derivatives / "index.tsv"
        if not index_path.exists():
            self._all_scan_ids = []
            self._audit_rows = []
            self.scanCombo.clear()
            self.autoLabel.text = "Auto grade: - | confidence: -"
            self.agreementLabel.text = "Agreement: overlap=0 | match=- | exact=-"
            self.trainingRevealLabel.text = "Last training reveal: -"
            self.clearReviewerCombo.clear()
            self.clearReviewerCombo.addItem(self.CLEAR_ALL_OPERATORS)
            self.agreementMatrixTable.setRowCount(0)
            self.agreementMatrixTable.setColumnCount(0)
            self._clear_profile_plot()
            self._set_run_scope_items(self._discover_scan_ids_for_dataset())
            self._update_review_queue_label()
            self._log(f"[review] index not found: {index_path}\n")
            return

        self._index_rows = {}
        self._review_rows = {}
        self._audit_rows = []

        with index_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                scan_id = row.get("scan_id", "")
                if not scan_id:
                    continue
                self._index_rows[scan_id] = row

                review_rel = row.get("review_tsv", "")
                if not review_rel:
                    continue
                review_path = derivatives / review_rel
                if not review_path.exists():
                    continue

                with review_path.open("r", encoding="utf-8", newline="") as rf:
                    rreader = csv.DictReader(rf, delimiter="\t")
                    for rrow in rreader:
                        rid = rrow.get("scan_id", "")
                        if rid:
                            self._review_rows[rid] = rrow

                audit_rel = row.get("review_audit", "")
                if audit_rel:
                    audit_path = derivatives / audit_rel
                    if audit_path.exists():
                        with audit_path.open("r", encoding="utf-8", newline="") as af:
                            areader = csv.DictReader(af, delimiter="\t")
                            for arow in areader:
                                self._audit_rows.append(arow)

        any_training = any(self._is_training_mode_row(row) for row in self._review_rows.values())
        self.trainingModeCheck.blockSignals(True)
        self.trainingModeCheck.setChecked(bool(any_training))
        self.trainingModeCheck.blockSignals(False)
        if not any_training:
            self.trainingRevealLabel.text = "Last training reveal: -"

        self._all_scan_ids = sorted(set(self._index_rows.keys()).union(set(self._review_rows.keys())))
        pending = self._pending_scan_ids()
        self._set_run_scope_items(pending)
        self._refresh_clear_reviewer_items()
        self._rebuild_scan_combo()
        self._update_review_queue_label()

        self._log(f"[review] loaded {len(self._review_rows)} scan review row(s), pending={len(pending)}\n")
        self._update_agreement_summary()
        self._update_agreement_matrix()
        self.onScanSelectionChanged(self._combo_text(self.scanCombo))

    def onTrainingModeToggled(self, checked):
        mode = "ON" if bool(checked) else "OFF"
        self._log(f"[review] training mode toggled: {mode}\n")
        derivatives = self._derivatives_root()
        if derivatives is None:
            self.onScanSelectionChanged(self._combo_text(self.scanCombo))
            return

        index_path = derivatives / "index.tsv"
        if not index_path.exists():
            # Nothing initialized yet: only refresh visible text state.
            self.onScanSelectionChanged(self._combo_text(self.scanCombo))
            return

        # Apply the mode switch to existing review rows immediately so pending scans
        # are properly blinded/unblinded without requiring a new predict run.
        args = [
            "review-init",
            str(derivatives),
            "--confidence-threshold",
            str(int(self.confidenceSpin.value)),
        ]
        if bool(checked):
            args.append("--training-mode")
        self._run_cli(args, on_finish=self.refreshReview)

    def onReviewScopeChanged(self, _scope_text):
        self._rebuild_scan_combo(preferred_scan_id=self._combo_text(self.scanCombo))
        self._update_review_queue_label()

    def onConfidenceThresholdChanged(self, *_args):
        if self.logic.is_running():
            return
        self._rebuild_scan_combo(preferred_scan_id=self._combo_text(self.scanCombo))
        self._update_review_queue_label()

    def _update_review_queue_label(self):
        shown_count = len(self._scan_ids_for_scope())
        pending_count = len(self._pending_scan_ids())
        reviewed_count = 0
        for row in self._review_rows.values():
            manual = str(row.get("manual_grade", "")).strip()
            if manual:
                reviewed_count += 1
        total_count = len(self._all_scan_ids)
        self.reviewQueueLabel.text = (
            f"Queue: shown={shown_count} | pending={pending_count} | reviewed={reviewed_count}/{total_count}"
        )

    def _pending_scan_ids(self):
        pending = []
        for scan_id, row in self._review_rows.items():
            status = str(row.get("review_status", "")).strip()
            is_training = self._is_training_mode_row(row)
            manual_set = bool(str(row.get("manual_grade", "")).strip())
            if status == "pending":
                pending.append(scan_id)
                continue
            if status == "training_pending":
                pending.append(scan_id)
                continue
            if is_training and not manual_set:
                pending.append(scan_id)
        return sorted(set(pending))

    def _low_confidence_scan_ids(self):
        threshold = int(self.confidenceSpin.value)
        out = []
        for scan_id in self._all_scan_ids:
            row = self._review_rows.get(scan_id) or self._index_rows.get(scan_id) or {}
            raw_conf = str(row.get("automatic_confidence", "")).strip()
            try:
                conf = float(raw_conf)
            except Exception:
                # If confidence is unavailable/corrupt, keep it visible for manual review.
                out.append(scan_id)
                continue
            if conf < threshold:
                out.append(scan_id)
        return out

    def _scan_ids_for_scope(self):
        scope = self._combo_text(self.reviewScopeCombo)
        if scope == self.REVIEW_SCOPE_ALL:
            return self._low_confidence_scan_ids()
        return self._pending_scan_ids()

    def _rebuild_scan_combo(self, preferred_scan_id=None):
        scan_ids = self._scan_ids_for_scope()
        current = str(preferred_scan_id or "").strip() or self._combo_text(self.scanCombo)
        self.scanCombo.blockSignals(True)
        self.scanCombo.clear()
        self.scanCombo.addItems(scan_ids)
        if current and current in scan_ids:
            self.scanCombo.setCurrentIndex(scan_ids.index(current))
        elif scan_ids:
            self.scanCombo.setCurrentIndex(0)
        self.scanCombo.blockSignals(False)
        self.onScanSelectionChanged(self._combo_text(self.scanCombo))
        self._update_review_queue_label()

    def _refresh_clear_reviewer_items(self):
        previous = self._combo_text(self.clearReviewerCombo)
        reviewers = sorted(
            {
                str(row.get("reviewer", "")).strip()
                for row in self._review_rows.values()
                if str(row.get("reviewer", "")).strip()
            }
        )
        items = [self.CLEAR_ALL_OPERATORS] + reviewers
        self.clearReviewerCombo.blockSignals(True)
        self.clearReviewerCombo.clear()
        self.clearReviewerCombo.addItems(items)
        if previous in items:
            self.clearReviewerCombo.setCurrentIndex(items.index(previous))
        elif previous and previous != self.CLEAR_ALL_OPERATORS:
            self.clearReviewerCombo.setEditText(previous)
        else:
            self.clearReviewerCombo.setCurrentIndex(0)
        self.clearReviewerCombo.blockSignals(False)

    def onDatasetPathChanged(self, *_args):
        if self.logic.is_running():
            return
        self._grade_history = []
        self.backButton.enabled = False
        if self._review_rows:
            self._set_run_scope_items(self._pending_scan_ids())
        else:
            self._set_run_scope_items(self._discover_scan_ids_for_dataset())
        self._update_review_queue_label()

    def onScanSelectionChanged(self, scan_id):
        if not scan_id:
            self.autoLabel.text = "Auto grade: - | confidence: -"
            self._set_selected_manual_grade(None)
            self._clear_profile_plot()
            return

        row = self._review_rows.get(scan_id)
        if row is None:
            self.autoLabel.text = "Auto grade: - | confidence: -"
            self._set_selected_manual_grade(None)
            self._clear_profile_plot()
            return

        auto_grade = row.get("automatic_grade", "-")
        auto_conf = row.get("automatic_confidence", "-")
        training_pending = self._is_training_mode_row(row) and not str(row.get("manual_grade", "")).strip()
        blind_active = bool(self.trainingModeCheck.checked) or training_pending
        if blind_active:
            self.autoLabel.text = "Training mode: prediction hidden until manual grade is submitted."
            self._set_selected_manual_grade(None)
            self._show_profile_whiteout()
        else:
            self.autoLabel.text = f"Auto grade: {auto_grade} | confidence: {auto_conf}%"
            self._update_profile_plot(scan_id)
        if not blind_active:
            try:
                self._set_selected_manual_grade(int(float(auto_grade)), suggested=True)
            except Exception:
                self._set_selected_manual_grade(None)

        if (
            scan_id
            and self._auto_load_enabled()
            and scan_id != self._loaded_scan_id
            and not self.logic.is_running()
            and not self._auto_loading_scan
        ):
            qt.QTimer.singleShot(0, self._auto_load_current_scan)

    def _auto_load_current_scan(self):
        if self._auto_loading_scan or not self._auto_load_enabled():
            return
        scan_id = self._combo_text(self.scanCombo)
        if not scan_id or scan_id == self._loaded_scan_id:
            return
        self._auto_loading_scan = True
        try:
            self.onLoadSelectedScan()
        finally:
            self._auto_loading_scan = False

    def onQuickSelectGrade(self, grade):
        try:
            grade_value = int(grade)
        except Exception:
            return
        if grade_value < 1 or grade_value > 5:
            return
        if not self._combo_text(self.scanCombo):
            return
        self._set_selected_manual_grade(grade_value)
        self.onApplyManual()

    def _focus_scan_for_edit(self, scan_id):
        if not scan_id:
            return False
        idx = self.scanCombo.findText(scan_id)
        self.scanCombo.blockSignals(True)
        if idx < 0:
            self.scanCombo.insertItem(0, scan_id)
            idx = 0
        self.scanCombo.setCurrentIndex(idx)
        self.scanCombo.blockSignals(False)
        self.onScanSelectionChanged(scan_id)
        self.onLoadSelectedScan()
        return True

    def onBackToPreviousScan(self):
        if not self._grade_history:
            return
        scan_id = self._grade_history.pop()
        self.backButton.enabled = bool(self._grade_history)
        if scan_id not in self._index_rows:
            self.refreshReview()
        if scan_id not in self._index_rows:
            slicer.util.errorDisplay(f"Cannot go back: scan '{scan_id}' is no longer available in this dataset.")
            return
        self._focus_scan_for_edit(scan_id)

    def onApplyManual(self):
        scan_id = self._combo_text(self.scanCombo)
        if not scan_id:
            slicer.util.errorDisplay("No pending scan selected")
            return

        reviewer = self.reviewerEdit.text.strip()
        if not reviewer:
            slicer.util.errorDisplay("Please enter Reviewer")
            return
        self._persist_reviewer_setting()

        manual_grade = int(self._selected_manual_grade or 0)
        if manual_grade < 1 or manual_grade > 5:
            slicer.util.errorDisplay("Please select a quick grade (1 to 5) before saving")
            return

        derivatives = self._derivatives_root()
        if derivatives is None:
            slicer.util.errorDisplay("Cannot resolve results root")
            return

        args = [
            "review-apply",
            str(derivatives),
            "--scan-id",
            scan_id,
            "--manual-grade",
            str(manual_grade),
            "--reviewer",
            reviewer,
        ]
        self._run_cli(args, on_finish=lambda sid=scan_id: self._on_manual_applied(sid))

    def _on_manual_applied(self, scan_id):
        if scan_id:
            self._grade_history.append(scan_id)
        self.backButton.enabled = bool(self._grade_history)
        self._refresh_and_load_next(previous_scan_id=scan_id)

    def onClearGrades(self):
        derivatives = self._derivatives_root()
        if derivatives is None:
            slicer.util.errorDisplay("Cannot resolve results root")
            return

        selected_reviewer = self._combo_text(self.clearReviewerCombo)
        args = [
            "review-clear",
            str(derivatives),
        ]
        if selected_reviewer == self.CLEAR_ALL_OPERATORS:
            args.append("--all-reviewers")
            msg = "Clear grades for ALL reviewers?"
        elif selected_reviewer:
            args.extend(["--reviewer", selected_reviewer])
            msg = f"Clear all grades for reviewer '{selected_reviewer}'?"
        else:
            slicer.util.errorDisplay("Select a reviewer to clear, or choose 'All operators'.")
            return

        if not slicer.util.confirmYesNoDisplay(msg, windowTitle="Clear Grades"):
            return
        self._run_cli(args, on_finish=self.refreshReview)

    def onExport(self):
        derivatives = self._derivatives_root()
        if derivatives is None:
            slicer.util.errorDisplay("Cannot resolve results root")
            return

        args = [
            "export",
            str(derivatives),
        ]
        self._run_cli(args)

    def onLoadSelectedScan(self):
        scan_id = self._combo_text(self.scanCombo)
        if not scan_id:
            slicer.util.errorDisplay("No pending scan selected")
            return

        index_row = self._index_rows.get(scan_id)
        if index_row is None:
            slicer.util.errorDisplay(f"Missing index row for {scan_id}")
            return

        raw_path = index_row.get("raw_image_path", "").strip()
        if not raw_path:
            slicer.util.errorDisplay(f"Missing raw image path for {scan_id}")
            return

        row = self._review_rows.get(scan_id, {})
        training_pending = self._is_training_mode_row(row) and not str(row.get("manual_grade", "")).strip()
        blind_active = bool(self.trainingModeCheck.checked) or training_pending
        if blind_active:
            self._show_profile_whiteout()
        else:
            # Always try to update the profile plot, even if volume load later fails.
            self._update_profile_plot(scan_id)

        # Avoid keeping many large scans in memory: unload prior scan before loading next.
        self._remove_loaded_scan_volume()

        primary_error = None
        loaded_node = None
        try:
            loaded_node = slicer.util.loadVolume(raw_path)
        except Exception as exc:
            primary_error = exc
            self._log(f"[load] native volume load failed for {raw_path}: {exc}\n")

        if loaded_node is None or loaded_node is False:
            try:
                loaded_node = self._load_aim_with_core(raw_path)
                self._log(f"[load] AIM fallback load succeeded: {raw_path}\n")
            except Exception as fallback_exc:
                if primary_error is not None:
                    slicer.util.errorDisplay(
                        "Could not load selected scan.\n"
                        f"Path: {raw_path}\n"
                        f"Native Slicer load error: {primary_error}\n"
                        f"AIM fallback load error: {fallback_exc}"
                    )
                else:
                    slicer.util.errorDisplay(
                        "Could not load selected scan.\n"
                        f"Path: {raw_path}\n"
                        f"AIM fallback load error: {fallback_exc}"
                    )
                return

        self._log(f"[load] loaded volume: {raw_path}\n")
        self._loaded_scan_id = scan_id
        self._loaded_volume_node = loaded_node
        self._render_profile_plot(scan_id)

    def _remove_loaded_scan_volume(self):
        node = self._loaded_volume_node
        self._loaded_volume_node = None
        self._loaded_scan_id = ""
        if node is None:
            return
        try:
            if node.GetScene() is not None:
                slicer.mrmlScene.RemoveNode(node)
                self._log(f"[load] removed previous volume: {node.GetName()}\n")
        except Exception as exc:
            self._log(f"[load] failed to remove previous volume: {exc}\n")

    def _refresh_and_load_next(self, previous_scan_id=None):
        self.refreshReview()
        self._show_training_reveal(previous_scan_id)
        count_attr = self.scanCombo.count
        count = int(count_attr() if callable(count_attr) else count_attr)
        if count <= 0:
            self._clear_profile_plot()
            return

        idx = -1
        if previous_scan_id:
            idx = self.scanCombo.findText(previous_scan_id)
            if idx >= 0 and count > 1:
                idx = min(idx + 1, count - 1)
        if idx < 0:
            idx = 0
        self.scanCombo.setCurrentIndex(idx)
        if not self._auto_load_enabled():
            self.onLoadSelectedScan()

    def _set_run_scope_items(self, scan_ids):
        previous = self._combo_text(self.runScopeCombo)
        items = [self.RUN_SCOPE_ALL] + list(scan_ids)
        self.runScopeCombo.blockSignals(True)
        self.runScopeCombo.clear()
        self.runScopeCombo.addItems(items)
        if previous in items:
            self.runScopeCombo.setCurrentIndex(items.index(previous))
        else:
            self.runScopeCombo.setCurrentIndex(0)
        self.runScopeCombo.blockSignals(False)

    def _discover_scan_ids_for_dataset(self):
        dataset = self.datasetPathEdit.currentPath.strip()
        if not dataset:
            return []
        try:
            from motionscore.config import AppConfig
            from motionscore.dataset.discovery import discover_raw_sessions
            from motionscore.utils import make_scan_id
        except Exception:
            return []

        try:
            sessions = discover_raw_sessions(root=Path(dataset), cfg=AppConfig().discovery)
            scan_ids = [
                make_scan_id(s.subject_id, s.site, s.session_id, s.raw_image_path)
                for s in sessions
            ]
            return sorted(set(scan_ids))
        except Exception as exc:
            self._log(f"[run-scope] could not discover scans for dropdown: {exc}\n")
            return []

    def _all_scans_already_predicted(self):
        derivatives = self._derivatives_root()
        if derivatives is None:
            return False
        index_path = derivatives / "index.tsv"
        if not index_path.exists():
            return False

        discovered_scan_ids = set(self._discover_scan_ids_for_dataset())
        if not discovered_scan_ids:
            return False

        indexed_scan_ids = set()
        try:
            with index_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f, delimiter="\t")
                for row in reader:
                    scan_id = str(row.get("scan_id", "")).strip()
                    if scan_id:
                        indexed_scan_ids.add(scan_id)
        except Exception as exc:
            self._log(f"[predict] could not read existing index for skip-check: {exc}\n")
            return False

        return discovered_scan_ids.issubset(indexed_scan_ids)

    def _on_process_output(self, text):
        self._log(text)
        if self._active_task_name != "predict":
            return
        self._update_predict_progress_from_output(text)

    def _combo_count(self, combo):
        count_attr = combo.count
        return int(count_attr() if callable(count_attr) else count_attr)

    def _combo_text(self, combo):
        text_attr = combo.currentText
        text = text_attr() if callable(text_attr) else text_attr
        return str(text).strip()

    def _start_predict_progress(self, selected_scope):
        if selected_scope and selected_scope != self.RUN_SCOPE_ALL:
            total = 1
        else:
            total = max(0, self._combo_count(self.runScopeCombo) - 1)

        self._predict_total = int(total)
        self._predict_done = 0
        if self._predict_total > 0:
            self.progressBar.minimum = 0
            self.progressBar.maximum = self._predict_total
            self.progressBar.value = 0
            self.progressLabel.setText(f"Processing scan 0/{self._predict_total}")
        else:
            # Unknown total count: show indeterminate progress.
            self.progressBar.minimum = 0
            self.progressBar.maximum = 0
            self.progressBar.value = 0
            self.progressLabel.setText("Processing scans...")

    def _update_predict_progress_from_output(self, text):
        completed = len(re.findall(r"(?m)^\[predict\]\s+", text))
        if completed <= 0:
            return
        self._predict_done += completed

        if self._predict_total > 0:
            current = min(self._predict_done, self._predict_total)
            self.progressBar.minimum = 0
            self.progressBar.maximum = self._predict_total
            self.progressBar.value = current
            self.progressLabel.setText(f"Processing scan {current}/{self._predict_total}")
        else:
            self.progressBar.minimum = 0
            self.progressBar.maximum = 0
            self.progressBar.value = 0
            self.progressLabel.setText(f"Processing scans... done {self._predict_done}")

    def _set_progress_idle(self):
        self._predict_total = 0
        self._predict_done = 0
        self.progressBar.minimum = 0
        self.progressBar.maximum = 100
        self.progressBar.value = 0
        self.progressLabel.setText("Idle")

    def _load_aim_with_core(self, raw_path):
        from motionscore.io.aim import read_aim

        aim = read_aim(Path(raw_path), scaling="native")
        volume_xyz = aim.data
        volume_kji = volume_xyz.transpose(2, 1, 0)

        node_name = Path(raw_path).stem
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", node_name)
        slicer.util.updateVolumeFromArray(node, volume_kji)

        try:
            sx, sy, sz = aim.spacing
            node.SetSpacing(float(sx), float(sy), float(sz))
        except Exception:
            pass
        try:
            ox, oy, oz = aim.origin
            node.SetOrigin(float(ox), float(oy), float(oz))
        except Exception:
            pass

        slicer.util.setSliceViewerLayers(background=node)
        slicer.util.resetSliceViews()
        return node

    def _clear_profile_plot(self):
        self._profile_source_pixmap = None
        self._profile_scan_id = ""
        self.profileLabel.clear()
        self.profileLabel.setText("Slice profile plot: -")

    def _show_profile_whiteout(self):
        self._profile_source_pixmap = None
        self._profile_scan_id = ""
        self.profileLabel.clear()
        self.profileLabel.setText("Slice profile hidden until manual grade is submitted.")

    def _is_training_mode_row(self, row):
        text = str(row.get("training_mode", "")).strip().lower()
        return text in {"1", "true", "yes", "y", "on"}

    def _update_agreement_summary(self):
        try:
            from motionscore.review.store import compute_review_agreement
        except Exception as exc:
            self.agreementLabel.text = "Agreement: unavailable (core import failed)"
            self._log(f"[agreement] failed to import compute_review_agreement: {exc}\n")
            return

        stats = compute_review_agreement(list(self._review_rows.values()))
        n_scored = int(stats.get("n_scored", 0))
        if n_scored <= 0:
            self.agreementLabel.text = "Agreement: overlap=0 | match=- | exact=-"
            return

        exact_matches = 0
        for row in self._review_rows.values():
            auto_txt = str(row.get("automatic_grade", "")).strip()
            manual_txt = str(row.get("manual_grade", "")).strip()
            if not auto_txt or not manual_txt:
                continue
            try:
                if int(float(auto_txt)) == int(float(manual_txt)):
                    exact_matches += 1
            except Exception:
                continue
        exact = float(stats.get("agreement_exact", 0.0)) * 100.0
        self.agreementLabel.text = f"Agreement: overlap={n_scored} | match={exact_matches}/{n_scored} | exact={exact:.1f}%"

    def _update_agreement_matrix(self):
        try:
            from motionscore.review.store import compute_grade_pair_agreement
        except Exception as exc:
            self._log(f"[agreement] failed to import compute_grade_pair_agreement: {exc}\n")
            self.agreementMatrixTable.setRowCount(0)
            self.agreementMatrixTable.setColumnCount(0)
            return

        def _canon_reviewer(text):
            value = str(text or "").strip()
            if not value:
                return ""
            value = re.sub(r"\s+", " ", value)
            return value.casefold()

        # AI reference grades from current review table.
        ai_by_scan = {}
        for scan_id, row in self._review_rows.items():
            try:
                grade = int(float(str(row.get("automatic_grade", "")).strip()))
            except Exception:
                continue
            if 1 <= grade <= 5:
                ai_by_scan[scan_id] = grade

        # Build operator grades from current review rows first (most reliable snapshot),
        # then overlay audit history to preserve clear/apply ordering when available.
        op_scan_grade = {}
        reviewer_label_by_canon = {}
        for scan_id, row in self._review_rows.items():
            reviewer_raw = str(row.get("reviewer", "")).strip()
            reviewer = _canon_reviewer(reviewer_raw)
            if not reviewer:
                continue
            reviewer_label_by_canon.setdefault(reviewer, reviewer_raw)
            try:
                grade = int(float(str(row.get("manual_grade", "")).strip()))
            except Exception:
                continue
            if 1 <= grade <= 5:
                op_scan_grade[(reviewer, scan_id)] = grade

        audit_rows = sorted(
            self._audit_rows,
            key=lambda row: str(row.get("timestamp", "")),
        )
        for row in audit_rows:
            scan_id = str(row.get("scan_id", "")).strip()
            reviewer_raw = str(row.get("reviewer", "")).strip()
            reviewer = _canon_reviewer(reviewer_raw)
            event = str(row.get("event", "")).strip()
            if not scan_id or not reviewer:
                continue
            reviewer_label_by_canon.setdefault(reviewer, reviewer_raw)
            if event == "clear_manual":
                op_scan_grade.pop((reviewer, scan_id), None)
                continue
            try:
                grade = int(float(str(row.get("manual_grade", "")).strip()))
            except Exception:
                continue
            if 1 <= grade <= 5:
                op_scan_grade[(reviewer, scan_id)] = grade

        operators = sorted({op for op, _sid in op_scan_grade.keys()}, key=lambda op: reviewer_label_by_canon.get(op, op))
        op_counts = {}
        for op in operators:
            op_counts[op] = len({sid for (op_key, sid), _g in op_scan_grade.items() if op_key == op})
        participant_ids = ["AI"] + operators
        participants = ["AI"] + [f"{reviewer_label_by_canon.get(op, op)} (graded={op_counts.get(op, 0)})" for op in operators]
        n = len(participant_ids)
        self.agreementMatrixTable.setRowCount(n)
        self.agreementMatrixTable.setColumnCount(n)
        self.agreementMatrixTable.setVerticalHeaderLabels(participants)
        self.agreementMatrixTable.setHorizontalHeaderLabels(participants)

        for i, p_i in enumerate(participant_ids):
            for j, p_j in enumerate(participant_ids):
                if i == j:
                    item = qt.QTableWidgetItem("—")
                    item.setTextAlignment(qt.Qt.AlignCenter)
                    self.agreementMatrixTable.setItem(i, j, item)
                    continue
                if j > i:
                    # show lower-triangular matrix only (avoid duplicate mirrored entries)
                    item = qt.QTableWidgetItem("")
                    item.setTextAlignment(qt.Qt.AlignCenter)
                    self.agreementMatrixTable.setItem(i, j, item)
                    continue

                pairs = []
                if p_i == "AI":
                    scan_to_grade_j = {
                        sid: g for (op, sid), g in op_scan_grade.items() if op == p_j
                    }
                    for sid, g_ai in ai_by_scan.items():
                        g_j = scan_to_grade_j.get(sid)
                        if g_j is not None:
                            pairs.append((g_ai, g_j))
                elif p_j == "AI":
                    scan_to_grade_i = {
                        sid: g for (op, sid), g in op_scan_grade.items() if op == p_i
                    }
                    for sid, g_ai in ai_by_scan.items():
                        g_i = scan_to_grade_i.get(sid)
                        if g_i is not None:
                            pairs.append((g_i, g_ai))
                else:
                    scan_to_grade_i = {
                        sid: g for (op, sid), g in op_scan_grade.items() if op == p_i
                    }
                    scan_to_grade_j = {
                        sid: g for (op, sid), g in op_scan_grade.items() if op == p_j
                    }
                    common = set(scan_to_grade_i.keys()).intersection(set(scan_to_grade_j.keys()))
                    for sid in sorted(common):
                        pairs.append((scan_to_grade_i[sid], scan_to_grade_j[sid]))

                stats = compute_grade_pair_agreement(pairs)
                n_scored = int(stats.get("n_scored", 0))
                if n_scored <= 0:
                    txt = "overlap=0"
                else:
                    exact_matches = sum(1 for g_i, g_j in pairs if g_i == g_j)
                    exact = float(stats.get("agreement_exact", 0.0)) * 100.0
                    txt = f"overlap={n_scored}\nmatch={exact_matches}/{n_scored}\nexact={exact:.1f}%"
                item = qt.QTableWidgetItem(txt)
                item.setTextAlignment(qt.Qt.AlignCenter)
                self.agreementMatrixTable.setItem(i, j, item)

        self.agreementMatrixTable.resizeColumnsToContents()
        self.agreementMatrixTable.resizeRowsToContents()

    def _show_training_reveal(self, scan_id):
        if not scan_id:
            return
        row = self._review_rows.get(scan_id)
        if row is None:
            return
        if not self._is_training_mode_row(row):
            return
        manual_grade = str(row.get("manual_grade", "")).strip()
        if not manual_grade:
            return
        auto_grade = str(row.get("automatic_grade", "")).strip() or "-"
        match = "match" if auto_grade == manual_grade else "mismatch"
        text = f"{scan_id}: operator={manual_grade}, AI={auto_grade} ({match})"
        self.trainingRevealLabel.text = f"Last training reveal: {text}"
        self._log(f"[training] reveal {text}\n")

    def _install_slice_observer(self):
        try:
            layout_manager = slicer.app.layoutManager()
            if layout_manager is None:
                return
            red_widget = layout_manager.sliceWidget("Red")
            if red_widget is None:
                return
            slice_node = red_widget.mrmlSliceNode()
            if slice_node is None:
                return
            if self._slice_observer_node is slice_node and self._slice_observer_tag is not None:
                return
            if self._slice_observer_node is not None and self._slice_observer_tag is not None:
                self._slice_observer_node.RemoveObserver(self._slice_observer_tag)
            self._slice_observer_node = slice_node
            self._slice_observer_tag = slice_node.AddObserver(
                slicer.vtkMRMLSliceNode.ModifiedEvent,
                self._on_slice_position_changed,
            )
        except Exception as exc:
            self._log(f"[profile] could not install slice observer: {exc}\n")

    def _on_slice_position_changed(self, _caller=None, _event=None):
        if not self._loaded_scan_id:
            return
        if self._profile_scan_id != self._loaded_scan_id:
            return
        self._render_profile_plot(self._loaded_scan_id)

    def _current_slice_cursor(self):
        node = self._loaded_volume_node
        if node is None:
            return None, None
        image_data = node.GetImageData() if hasattr(node, "GetImageData") else None
        if image_data is None:
            return None, None
        dims = image_data.GetDimensions()
        if len(dims) < 3:
            return None, None
        n_slices = int(dims[2])
        if n_slices <= 0:
            return None, None

        self._install_slice_observer()
        if self._slice_observer_node is None:
            return None, n_slices

        # Slicer API compatibility: some versions fill an output matrix argument,
        # newer versions return the matrix directly with no arguments.
        slice_to_ras = vtk.vtkMatrix4x4()
        try:
            self._slice_observer_node.GetSliceToRAS(slice_to_ras)
        except TypeError:
            returned = self._slice_observer_node.GetSliceToRAS()
            if returned is not None:
                slice_to_ras.DeepCopy(returned)
        ras_h = [
            float(slice_to_ras.GetElement(0, 3)),
            float(slice_to_ras.GetElement(1, 3)),
            float(slice_to_ras.GetElement(2, 3)),
            1.0,
        ]

        ras_to_ijk = vtk.vtkMatrix4x4()
        node.GetRASToIJKMatrix(ras_to_ijk)
        ijk_h = [0.0, 0.0, 0.0, 0.0]
        ras_to_ijk.MultiplyPoint(ras_h, ijk_h)
        slice_idx = int(round(float(ijk_h[2])))
        slice_idx = max(0, min(n_slices - 1, slice_idx))
        return slice_idx, n_slices

    def _grade_for_scan(self, scan_id):
        if self._selected_manual_grade in {1, 2, 3, 4, 5}:
            return int(self._selected_manual_grade)
        row = self._review_rows.get(scan_id, {})
        for key in ("manual_grade", "automatic_grade"):
            try:
                value = int(float(str(row.get(key, "")).strip()))
            except Exception:
                value = 0
            if 1 <= value <= 5:
                return value
        return 3

    def _render_profile_plot(self, scan_id):
        if self._profile_source_pixmap is None:
            self._clear_profile_plot()
            return
        if not scan_id or scan_id != self._profile_scan_id:
            return

        width_attr = self.profileLabel.width
        label_width = int(width_attr() if callable(width_attr) else width_attr)
        width = max(300, min(800, label_width - 8))
        scaled = self._profile_source_pixmap.scaledToWidth(width, qt.Qt.SmoothTransformation)
        if scaled.isNull():
            self._clear_profile_plot()
            return

        out_pix = qt.QPixmap(scaled)
        if scan_id == self._loaded_scan_id:
            slice_idx, n_slices = self._current_slice_cursor()
            if slice_idx is not None and n_slices is not None and n_slices > 1:
                frac = float(slice_idx) / float(n_slices - 1)
                y = int(round(frac * float(max(0, out_pix.height() - 1))))
                painter = qt.QPainter(out_pix)
                pen = qt.QPen(self._grade_plot_color(self._grade_for_scan(scan_id)))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawLine(0, y, max(0, out_pix.width() - 1), y)
                painter.end()

        self.profileLabel.setPixmap(out_pix)

    def _update_profile_plot(self, scan_id):
        derivatives = self._derivatives_root()
        if derivatives is None:
            self._clear_profile_plot()
            return

        row = self._index_rows.get(scan_id, {})
        rel_path = (
            row.get("slice_profile_png_path", "").strip()
            or row.get("preview_png_path", "").strip()
        )
        if not rel_path:
            self._log(f"[profile] no png path in index for {scan_id}\n")
            self._clear_profile_plot()
            return

        png_path = (derivatives / rel_path).resolve()
        if not png_path.exists():
            self._log(f"[profile] png not found on disk: {png_path}\n")
            self._clear_profile_plot()
            return

        try:
            pix = qt.QPixmap(str(png_path))
        except Exception as exc:
            self._log(f"[profile] failed to create QPixmap for {png_path}: {exc}\n")
            self._clear_profile_plot()
            return
        if pix.isNull():
            self._log(f"[profile] QPixmap is null for {png_path}\n")
            self._clear_profile_plot()
            return

        self._profile_source_pixmap = pix
        self._profile_scan_id = scan_id
        self._render_profile_plot(scan_id)
