import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import qt
import vtk

import slicer
from slicer import vtkMRMLScalarVolumeNode, vtkMRMLSegmentationNode
from slicer.i18n import tr as _
from slicer.parameterNodeWrapper import WithinRange, parameterNodeWrapper
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)
from slicer.util import VTKObservationMixin


#
# SegmentOrgansModule
#


class SegmentOrgansModule(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "1. Segmentation: organs"
        self.parent.categories = ["Quantification.Oncometer"]
        self.parent.dependencies = []
        self.parent.contributors = [
            "Charlotte Boucherie (Centre Henri Becquerel)",
            "Solene Perret (Centre Henri Becquerel)",
            "Pierre Decazes (Centre Henri Becquerel)",
        ]
        self.parent.helpText = _("""
        Organ segmentation among 9 categories using nnunetv2 on CT volumes.
        """)
        self.parent.acknowledgementText = _(
            """
            This file was originally developed by Charlotte Boucherie.
            """
        )

#
# SegmentOrgansModuleParameterNode
#


@parameterNodeWrapper
class SegmentOrgansModuleParameterNode:
    """Parameter node
    - inputVolumeCT: CT scan
    - outputSegmentation: output segmentation node
    """

    inputVolumeCT: vtkMRMLScalarVolumeNode | None = None
    outputSegmentation: vtkMRMLSegmentationNode | None = None

    datasetId: Annotated[int, WithinRange(0, 99999)] = 0
    configuration: str = "3d_fullres"
    trainer: str = "nnUNetTrainer"
    plansIdentifier: str = "nnUNetPlans"
    checkpointName: str = "checkpoint_final.pth"
    folds: str = "0,1,2,3,4"
    resultsFolder: str = ""


@dataclass(frozen=True)
class NnUNetModelPreset:
    name: str
    description: str
    dataset_id: int
    configuration: str = "3d_fullres"
    trainer: str = "nnUNetTrainer"
    plans_identifier: str = "nnUNetPlans"
    checkpoint_name: str = "checkpoint_final.pth"
    folds: tuple[str, ...] = ("0", "1", "2", "3", "4")
    results_folder: str = ""


DEFAULT_MODEL_PRESETS = (
    NnUNetModelPreset(
        name="ENHANCE-PET Moose Segmentation",
        description="ENHANCE-PET online model for organ segmentation on CT.",
        dataset_id=11,
    ),
)


#
# SegmentOrgansModuleWidget
#


class SegmentOrgansModuleWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None
        self._moosePreset: NnUNetModelPreset | None = None
        
    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        super().setup()

        # Load widget from .ui file (created by Qt Designer).
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/SegmentOrgansModule.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = SegmentOrgansModuleLogic()
        presets = self.logic.loadModelPresets()
        self._moosePreset = presets[0] if presets else None

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        if hasattr(self.ui, "modeComboBox"):
            self.ui.modeComboBox.connect("currentIndexChanged(int)", self.onModeChanged)

        modeHelpButton = getattr(self.ui, "modeHelpToolButton", None)
        if modeHelpButton is not None:
            modeHelpButton.connect("clicked(bool)", self.onModeHelpClicked)
        if hasattr(self.ui, "resultsFolderBrowseButton"):
            self.ui.resultsFolderBrowseButton.connect("clicked(bool)", self.onBrowseResultsFolderButton)

        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)

        if hasattr(self.ui, "modeComboBox"):
            try:
                self.ui.modeComboBox.setCurrentIndex(0)
            except Exception:
                pass
        self._updateModeUi()

        self.initializeParameterNode()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        self.initializeParameterNode()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None
            if self.hasObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply):
                self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

    def _widgetStringValue(self, widget, attributeName: str) -> str:
        attribute = getattr(widget, attributeName)
        return attribute() if callable(attribute) else attribute

    def _widgetIntValue(self, widget, attributeName: str) -> int:
        attribute = getattr(widget, attributeName)
        return attribute() if callable(attribute) else attribute

    @staticmethod
    def _processEvents() -> None:
        try:
            slicer.app.processEvents()
        except Exception:
            qt.QCoreApplication.processEvents()

    def _createBusyDialog(self, title: str, message: str) -> qt.QDialog:
        busyDialog = qt.QDialog(slicer.util.mainWindow() if hasattr(slicer.util, "mainWindow") else None)
        busyDialog.setWindowTitle(title)
        try:
            busyDialog.setWindowModality(qt.Qt.WindowModal)
        except Exception:
            pass
        try:
            busyDialog.setWindowFlags(busyDialog.windowFlags() & ~qt.Qt.WindowContextHelpButtonHint)
        except Exception:
            pass
        try:
            busyDialog.setAutoFillBackground(True)
        except Exception:
            pass
        try:
            busyDialog.setAttribute(qt.Qt.WA_TranslucentBackground, False)
        except Exception:
            pass

        busyLayout = qt.QVBoxLayout(busyDialog)
        busyLabel = qt.QLabel(message, busyDialog)
        busyBar = qt.QProgressBar(busyDialog)
        busyBar.setRange(0, 0)
        busyLayout.addWidget(busyLabel)
        busyLayout.addWidget(busyBar)
        busyDialog.setLayout(busyLayout)
        try:
            busyDialog.setMinimumSize(360, 120)
            busyDialog.resize(420, 140)
        except Exception:
            pass
        return busyDialog

    def _applyMoosePresetToParameterNode(self) -> None:
        """Applique le preset MOOSE unique (mode Simple) au ParameterNode."""
        if not self._parameterNode or self._moosePreset is None:
            return
        preset = self._moosePreset
        try:
            self._parameterNode.datasetId = int(preset.dataset_id)
            self._parameterNode.configuration = str(preset.configuration)
            self._parameterNode.trainer = str(preset.trainer)
            self._parameterNode.plansIdentifier = str(preset.plans_identifier)
            self._parameterNode.checkpointName = str(preset.checkpoint_name)
            self._parameterNode.folds = ",".join(preset.folds)
            self._parameterNode.resultsFolder = str(preset.results_folder)
        except Exception:
            pass

    def _ensureDefaultPresetApplied(self) -> None:
        """S'assure que le preset MOOSE est appliqué à l'ouverture, en mode Simple."""
        if not self._parameterNode or self._moosePreset is None:
            return

        modeIndex = 0
        if hasattr(self.ui, "modeComboBox"):
            try:
                modeIndex = int(self.ui.modeComboBox.currentIndex)
            except Exception:
                try:
                    modeIndex = int(self.ui.modeComboBox.currentIndex())
                except Exception:
                    modeIndex = 0
        if modeIndex != 0:
            return

        try:
            if int(self._parameterNode.datasetId) > 0:
                return
        except Exception:
            pass

        self._applyMoosePresetToParameterNode()

    def _modeStackWidget(self):
        return getattr(self.ui, "stackedModeWidget", getattr(self.ui, "simpleModeWidget", None))

    def _qtNumericAttribute(self, value, attributeName: str, default: int = 0) -> int:
        attribute = getattr(value, attributeName, None)
        if attribute is None:
            return default
        if callable(attribute):
            try:
                return int(attribute())
            except Exception:
                return default
        try:
            return int(attribute)
        except Exception:
            return default

    def _modePageContentHeight(self, page) -> int:
        if page is None:
            return 0

        childrenRectAttribute = getattr(page, "childrenRect", None)
        childrenRect = childrenRectAttribute() if callable(childrenRectAttribute) else childrenRectAttribute
        if childrenRect:
            top = self._qtNumericAttribute(childrenRect, "y", 0)
            height = self._qtNumericAttribute(childrenRect, "height", 0)
            if height > 0:
                return top + height + 4

        sizeHintAttribute = getattr(page, "sizeHint", None)
        sizeHintValue = sizeHintAttribute() if callable(sizeHintAttribute) else sizeHintAttribute
        if sizeHintValue is not None:
            return max(0, self._qtNumericAttribute(sizeHintValue, "height", 0))

        return 0

    def _updateModeUi(self) -> None:
        stack = self._modeStackWidget()
        if not stack or not hasattr(self.ui, "modeComboBox"):
            return

        try:
            modeIndex = int(self.ui.modeComboBox.currentIndex)
        except Exception:
            try:
                modeIndex = int(self.ui.modeComboBox.currentIndex())
            except Exception:
                modeIndex = 0

        stack.setCurrentIndex(modeIndex)
        stack.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Fixed)

        currentPage = stack.currentWidget()
        contentHeight = max(60, self._modePageContentHeight(currentPage))
        stack.setMinimumHeight(contentHeight)
        stack.setMaximumHeight(contentHeight)

        try:
            self.ui.modelCollapsibleButton.layout().invalidate()
            self.ui.modelCollapsibleButton.adjustSize()
        except Exception:
            pass

    def onModeChanged(self, index: int) -> None:
        if index == 0:
            self._applyMoosePresetToParameterNode()
        self._updateModeUi()
        self._checkCanApply()

    def onBrowseResultsFolderButton(self) -> None:
        folderPath = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(), "Select nnUNetv2 results folder"
        )
        if folderPath and hasattr(self.ui, "resultsFolderLineEdit"):
            self.ui.resultsFolderLineEdit.setText(folderPath)
            self._checkCanApply()

    def _showHelpMessage(self, title: str, message: str) -> None:
        try:
            qt.QMessageBox.information(slicer.util.mainWindow(), title, message)
        except Exception:
            slicer.util.showStatusMessage(message, 8000)

    def onModeHelpClicked(self, checked: bool = False) -> None:
        self._showHelpMessage(
            _("Model modes"),
            _(
                "- \"MOOSE\" mode uses the ENHANCE-PET Moose Segmentation model (default, recommended).\n"
                "- If you want to use your own model, use \"Custom nnUNet\" mode. You have to fill in the parameters of the model you want to use."
            ),
        )


    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        self.setParameterNode(self.logic.getParameterNode())

        if not self._parameterNode.inputVolumeCT:
            firstVolumeNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
            if firstVolumeNode:
                self._parameterNode.inputVolumeCT = firstVolumeNode

        self._ensureDefaultPresetApplied()

        self._checkCanApply()

    def setParameterNode(self, inputParameterNode: SegmentOrgansModuleParameterNode | None) -> None:
        """
        Set and observe parameter node.
        """
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            if self.hasObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply):
                self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
            self._checkCanApply()

    def _checkCanApply(self, caller=None, event=None) -> None:
        canApply = bool(self._parameterNode and self._parameterNode.inputVolumeCT and self._parameterNode.datasetId > 0)
        if canApply:
            self.ui.applyButton.toolTip = _("Run nnUNetv2 prediction")
            self.ui.applyButton.enabled = True
        else:
            self.ui.applyButton.toolTip = _("Select a CT and set the model dataset ID")
            self.ui.applyButton.enabled = False

    def _currentModelConfiguration(self) -> NnUNetModelPreset:
        modeIndex = 0
        if hasattr(self.ui, "modeComboBox"):
            try:
                modeIndex = int(self.ui.modeComboBox.currentIndex)
            except Exception:
                try:
                    modeIndex = int(self.ui.modeComboBox.currentIndex())
                except Exception:
                    modeIndex = 0

        if modeIndex == 0 and self._moosePreset is not None:
            return self._moosePreset

        datasetId = self._widgetIntValue(self.ui.datasetIdSpinBox, "value") if hasattr(self.ui, "datasetIdSpinBox") else 0
        configuration = self._widgetStringValue(self.ui.configurationComboBox, "currentText") if hasattr(self.ui, "configurationComboBox") else "3d_fullres"
        trainer = self._widgetStringValue(self.ui.trainerLineEdit, "text") if hasattr(self.ui, "trainerLineEdit") else "nnUNetTrainer"
        plansIdentifier = self._widgetStringValue(self.ui.plansIdentifierLineEdit, "text") if hasattr(self.ui, "plansIdentifierLineEdit") else "nnUNetPlans"
        checkpointName = self._widgetStringValue(self.ui.checkpointNameLineEdit, "text") if hasattr(self.ui, "checkpointNameLineEdit") else "checkpoint_final.pth"
        foldsText = self._widgetStringValue(self.ui.foldsLineEdit, "text") if hasattr(self.ui, "foldsLineEdit") else "0,1,2,3,4"
        resultsFolder = self._widgetStringValue(self.ui.resultsFolderLineEdit, "text") if hasattr(self.ui, "resultsFolderLineEdit") else ""
        folds = tuple(SegmentOrgansModuleLogic.parseFoldsText(foldsText))
        return NnUNetModelPreset(
            name="Custom nnUNet",
            description="",
            dataset_id=int(datasetId),
            configuration=configuration,
            trainer=trainer,
            plans_identifier=plansIdentifier,
            checkpoint_name=checkpointName,
            folds=folds,
            results_folder=resultsFolder,
        )

    def onApplyButton(self) -> None:
        """Run processing when user clicks "Apply" button."""
        busyDialog = self._createBusyDialog(_("Organ segmentation"), _("Segmentation in progress..."))
        try:
            busyDialog.show()
            busyDialog.raise_()
            busyDialog.activateWindow()
            busyDialog.repaint()
        except Exception:
            pass
        self._processEvents()

        # Run heavy work on the next event loop turn so the dialog has time to paint.
        def _run() -> None:
            completed = False
            outputSegmentation = self.ui.outputSegmentationSelector.currentNode()
            try:
                self.ui.applyButton.enabled = False
                with slicer.util.tryWithErrorDisplay(_("Failed to run organ segmentation."), waitCursor=True):
                    modeIndex = 0
                    if hasattr(self.ui, "modeComboBox"):
                        try:
                            modeIndex = int(self.ui.modeComboBox.currentIndex)
                        except Exception:
                            try:
                                modeIndex = int(self.ui.modeComboBox.currentIndex())
                            except Exception:
                                modeIndex = 0

                    presetName = self._moosePreset.name if (modeIndex == 0 and self._moosePreset) else None

                    if not outputSegmentation:
                        outputName = self.logic.defaultOutputSegmentationName(
                            self.ui.inputSelector.currentNode(),
                            presetName,
                        )
                        outputSegmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", outputName)
                        self.ui.outputSegmentationSelector.setCurrentNode(outputSegmentation)

                    configuration = self._currentModelConfiguration()

                    resultsFolder = configuration.results_folder
                    if modeIndex == 0:
                        bundledRoot = self.logic.bundledResultsRoot() if self.logic else ""
                        if bundledRoot:
                            resultsFolder = bundledRoot

                    self.logic.process(
                        inputVolumeCT=self.ui.inputSelector.currentNode(),
                        outputSegmentation=outputSegmentation,
                        datasetId=configuration.dataset_id,
                        configuration=configuration.configuration,
                        trainer=configuration.trainer,
                        plansIdentifier=configuration.plans_identifier,
                        checkpointName=configuration.checkpoint_name,
                        foldsText=",".join(configuration.folds),
                        resultsFolder=resultsFolder,
                        disableTTA=False,
                        stepSize=0.5,
                        showResult=True,
                        modelName=presetName,
                    )
                    completed = True
            finally:
                try:
                    busyDialog.close()
                except Exception:
                    pass
                try:
                    self.ui.applyButton.enabled = True
                except Exception:
                    pass

            if completed:
                self._showHelpMessage(
                    _("Prediction completed"),
                    _("Prediction finished.\nSegmentation: {segmentationName}").format(
                        segmentationName=outputSegmentation.GetName() if outputSegmentation else ""
                    ),
                )

        qt.QTimer.singleShot(100, _run)


#
# SegmentOrgansModuleLogic
#


class SegmentOrgansModuleLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def parseFoldsText(folds_text: str) -> list[str]:
        normalized = folds_text.strip().lower()
        if not normalized:
            raise ValueError("Fold list is empty")
        if normalized == "all":
            return ["all"]

        folds = [fold.strip() for fold in re.split(r"[;,\s]+", folds_text.strip()) if fold.strip()]
        if not folds:
            raise ValueError("Fold list is empty")

        for fold in folds:
            if fold != "all" and not fold.isdigit():
                raise ValueError(f"Invalid fold value: {fold}")
        return folds

    def getParameterNode(self):
        return SegmentOrgansModuleParameterNode(super().getParameterNode())

    def loadModelPresets(self) -> list[NnUNetModelPreset]:
        return list(DEFAULT_MODEL_PRESETS)

    def _sanitizeFileName(self, value: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
        return sanitized or "case"

    def defaultOutputSegmentationName(
        self, inputVolume: vtkMRMLScalarVolumeNode | None, presetName: str | None = None
    ) -> str:
        inputName = inputVolume.GetName() if inputVolume else "Segmentation"
        presetSuffix = f"_{self._sanitizeFileName(presetName)}" if presetName else ""
        return f"{self._sanitizeFileName(inputName)}{presetSuffix}_organs"

    def _bundledResultsRootCandidates(self) -> list[Path]:
        """Return candidate nnUNet_results roots shipped with the extension.

        Expected layout: Resources/Models_results/DatasetXXX_*/...
        """

        modelsRoot = Path(__file__).resolve().parent / "Resources" / "Models_results"
        return [modelsRoot] if modelsRoot.is_dir() else []

    def bundledResultsRoot(self) -> str:
        """Absolute path to the extension-bundled nnUNet_results root (if available)."""

        candidates = self._bundledResultsRootCandidates()
        return str(candidates[0]) if candidates else ""

    def _normalizeResultsRoot(self, root: Path, datasetId: int | None) -> Path | None:
        if not root or not root.is_dir():
            return None

        if datasetId is None:
            return root

        datasetPrefix = f"Dataset{int(datasetId):03d}_"

        if root.name.startswith(datasetPrefix) and root.parent.is_dir():
            return root.parent

        try:
            if any(path.is_dir() for path in root.glob(f"{datasetPrefix}*")):
                return root
        except Exception:
            pass

        return root

    def _resolveResultsFolder(self, explicitResultsFolder: str, datasetId: int | None = None) -> str:
        expanded = os.path.expandvars(os.path.expanduser((explicitResultsFolder or "").strip()))
        candidates: list[Path] = []
        if expanded:
            candidates.append(Path(expanded))

        envResults = os.environ.get("nnUNet_results", "").strip()
        if envResults:
            candidates.append(Path(envResults))

        candidates.extend(self._bundledResultsRootCandidates())

        for candidate in candidates:
            normalized = self._normalizeResultsRoot(candidate, datasetId)
            if normalized and normalized.is_dir():
                return str(normalized)

        return ""

    def _prepareSubprocessEnvironment(self, resultsFolder: str) -> dict[str, str]:
        env = os.environ.copy()
        if resultsFolder:
            env["nnUNet_results"] = resultsFolder
        return env

    def _predictCommand(self) -> list[str]:
        executable = shutil.which("nnUNetv2_predict")
        if executable:
            return [executable]
        return [sys.executable, "-m", "nnunetv2.inference.predict_from_raw_data"]

    def _runSubprocess(self, command: list[str], env: dict[str, str], cwd: str) -> subprocess.CompletedProcess:
        import selectors
        import time

        logging.info("Running command: %s", " ".join(command))
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        selector = selectors.DefaultSelector()
        try:
            if process.stdout is not None:
                selector.register(process.stdout, selectors.EVENT_READ)
            if process.stderr is not None:
                selector.register(process.stderr, selectors.EVENT_READ)

            lastPump = 0.0
            while True:
                now = time.time()
                # Keep Qt event loop alive while subprocess runs to prevent frozen progress dialogs.
                if now - lastPump >= 0.05:
                    try:
                        slicer.app.processEvents()
                    except Exception:
                        qt.QCoreApplication.processEvents()
                    lastPump = now

                returnCode = process.poll()
                if returnCode is not None:
                    break

                events = selector.select(timeout=0.1)
                for key, _mask in events:
                    fileobj = key.fileobj
                    try:
                        line = fileobj.readline()
                    except Exception:
                        line = ""
                    if not line:
                        continue
                    if fileobj is process.stdout:
                        stdout_lines.append(line)
                        logging.info(line.rstrip())
                    else:
                        stderr_lines.append(line)
                        logging.warning(line.rstrip())

            out, err = process.communicate(timeout=1)
            if out:
                stdout_lines.append(out)
            if err:
                stderr_lines.append(err)

        finally:
            try:
                selector.close()
            except Exception:
                pass

        completed = subprocess.CompletedProcess(
            args=command,
            returncode=int(process.returncode or 0),
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Prediction subprocess failed.\n"
                f"Command: {' '.join(command)}\n"
                f"STDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
        return completed

    def _writeInputVolume(
        self,
        inputVolumeCT: vtkMRMLScalarVolumeNode,
        inputDirectory: Path,
        caseName: str,
    ) -> Path:
        inputFileCT = inputDirectory / f"{caseName}_0000.nii.gz"
        if not slicer.util.saveNode(inputVolumeCT, str(inputFileCT)):
            raise RuntimeError(f"Unable to save CT input volume to {inputFileCT}")
        return inputFileCT

    def _findPredictionFile(self, outputDirectory: Path, caseName: str) -> Path:
        expectedFile = outputDirectory / f"{caseName}.nii.gz"
        if expectedFile.exists():
            return expectedFile

        candidates = sorted(outputDirectory.glob("*.nii.gz"))
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise FileNotFoundError(f"No prediction file found in {outputDirectory}")

        raise FileNotFoundError(f"Multiple prediction files found in {outputDirectory}, expected a single case output")

    def _renameSegments(self, outputSegmentation: vtkMRMLSegmentationNode) -> None:
        segmentation = outputSegmentation.GetSegmentation()
        validLabels = {1, 2, 3, 4, 5, 6, 7, 8, 9}

        for i in range(segmentation.GetNumberOfSegments()):
            segId = segmentation.GetNthSegmentID(i)
            segment = segmentation.GetSegment(segId)
            currentName = segment.GetName() or ""
            match = re.search(r"(\d+)", currentName)
            if not match:
                continue
            labelIndex = int(match.group(1))
            if labelIndex in validLabels:
                segment.SetName(str(labelIndex))

    def _loadLabelMapAsSegmentation(self, labelMapPath: str, outputSegmentation: vtkMRMLSegmentationNode) -> None:
        labelMapNode = slicer.util.loadLabelVolume(labelMapPath)
        if not labelMapNode:
            raise RuntimeError(f"Unable to load predicted label map: {labelMapPath}")

        segmentation = outputSegmentation.GetSegmentation()
        while segmentation.GetNumberOfSegments() > 0:
            segmentation.RemoveSegment(segmentation.GetNthSegmentID(0))

        outputSegmentation.CreateDefaultDisplayNodes()
        slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelMapNode, outputSegmentation)
        self._renameSegments(outputSegmentation)
        outputSegmentation.GetDisplayNode().SetVisibility(True)

        slicer.mrmlScene.RemoveNode(labelMapNode)

    def _preferredAccelerator(self) -> str:
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            mpsBackend = getattr(torch.backends, "mps", None)
            if mpsBackend is not None and mpsBackend.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def _findMoosePredictionFile(self, outputDirectory: Path) -> Path:
        candidates = sorted(outputDirectory.rglob("*segmentation_*.nii.gz"))
        if candidates:
            return candidates[0]
        candidates = sorted(outputDirectory.rglob("*.nii.gz"))
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise FileNotFoundError(f"No Moose prediction file found in {outputDirectory}")
        raise FileNotFoundError(f"Multiple Moose prediction files found in {outputDirectory}")

    def _simplifyMooseLabelMap(self, outputPathsByModel: dict[str, str], outputDirectory: Path) -> Path:
        try:
            import numpy as np
            import SimpleITK as sitk
        except Exception as error:
            raise RuntimeError("SimpleITK and numpy are required to simplify Moose outputs.") from error

        categoryMappings = {
            "clin_ct_organs": {
                1: 5, 2: 5, 4: 7, 6: 2, 7: 2, 8: 3, 9: 8, 10: 8, 11: 8, 12: 8, 13: 8, 14: 4, 15: 1,
            },
            "clin_ct_cardiac": {
                1: 6,  # heart_myocardium
                2: 6,  # heart_atrium_left
                3: 6,  # heart_atrium_right
                4: 6,  # heart_ventricle_left
                5: 6,  # heart_ventricle_right
                # 6-13 exclus : aorta, iliac_artery_left/right, iliac_vena_left/right,
                # inferior_vena_cava, portal_splenic_vein, pulmonary_artery — pas des labels du "coeur"
            },
            "clin_ct_peripheral_bones": {},
            "clin_ct_ribs": {},
            "clin_ct_vertebrae": {},
        }

        combinedArray: np.ndarray | None = None
        referenceImage = None

        for modelName, outputPath in outputPathsByModel.items():
            if not Path(outputPath).exists():
                continue
            image = sitk.ReadImage(outputPath)
            array = sitk.GetArrayFromImage(image)

            if referenceImage is None:
                referenceImage = image
                combinedArray = np.zeros_like(array, dtype=np.uint8)
            elif combinedArray is None or combinedArray.shape != array.shape:
                raise RuntimeError("Moose outputs have incompatible shapes and cannot be merged.")

            mapping = categoryMappings.get(modelName, {})
            if modelName in {"clin_ct_peripheral_bones", "clin_ct_ribs", "clin_ct_vertebrae"}:
                mappedArray = np.where(array > 0, 9, 0).astype(np.uint8)
            elif mapping:
                mappedArray = np.zeros_like(array, dtype=np.uint8)
                for sourceLabel, targetLabel in mapping.items():
                    mappedArray[array == sourceLabel] = targetLabel
            else:
                mappedArray = np.zeros_like(array, dtype=np.uint8)

            combinedArray = mappedArray if combinedArray is None else np.maximum(combinedArray, mappedArray)

        if combinedArray is None or referenceImage is None:
            raise RuntimeError("Moose did not produce any usable outputs.")

        simplifiedImage = sitk.GetImageFromArray(combinedArray)
        simplifiedImage.CopyInformation(referenceImage)

        simplifiedPath = outputDirectory / "moose_simplified_labels.nii.gz"
        sitk.WriteImage(simplifiedImage, str(simplifiedPath))
        return simplifiedPath

    def _runMooseInference(
        self,
        inputVolumeCT: vtkMRMLScalarVolumeNode,
        outputSegmentation: vtkMRMLSegmentationNode,
        showResult: bool,
    ) -> vtkMRMLSegmentationNode:
        caseName = self._sanitizeFileName(inputVolumeCT.GetName() or "CT_scan")

        modelNames = [
            "clin_ct_organs",
            "clin_ct_cardiac",
            "clin_ct_peripheral_bones",
            "clin_ct_ribs",
            "clin_ct_vertebrae",
        ]

        with tempfile.TemporaryDirectory(prefix="SegmentOrgansModule_Moose_") as tempDirectory:
            tempRoot = Path(tempDirectory)
            subjectDirectory = tempRoot / caseName
            subjectDirectory.mkdir(parents=True)

            inputFile = subjectDirectory / f"CT_{caseName}.nii.gz"
            if not slicer.util.saveNode(inputVolumeCT, str(inputFile)):
                raise RuntimeError(f"Unable to save CT input volume to {inputFile}")

            moosezExecutable = shutil.which("moosez")
            if not moosezExecutable:
                raise RuntimeError(
                    "The moosez command is not available in PATH. Install moosez in the system Python environment used by the extension."
                )

            command = [moosezExecutable, "-d", str(tempRoot), "-m", *modelNames]

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["LANG"] = "C.UTF-8"
            env["LC_ALL"] = "C.UTF-8"
            self._runSubprocess(command, env=env, cwd=str(tempRoot))

            # DEBUG : liste tous les fichiers nii.gz réellement produits, pour diagnostic.
            allNiftiFiles = sorted(subjectDirectory.rglob("*.nii.gz"))
            logging.info("Moose produced %d nii.gz files:", len(allNiftiFiles))
            for f in allNiftiFiles:
                logging.info("  %s", f)

            # Recherche explicite, modèle par modèle, au lieu de réassocier une liste globale.
            outputPathsByModel: dict[str, str] = {}
            missingModels: list[str] = []
            for modelName in modelNames:
                matches = sorted(subjectDirectory.rglob(f"*{modelName}*"))
                matches = [m for m in matches if m.suffixes[-2:] == [".nii", ".gz"]]
                if not matches:
                    # Repli insensible à la casse : moosez peut nommer ses fichiers avec une casse
                    # différente (ex. "clin_CT_organs" au lieu de "clin_ct_organs").
                    modelNameLower = modelName.lower()
                    matches = sorted(
                        p for p in subjectDirectory.rglob("*.nii.gz")
                        if modelNameLower in p.name.lower()
                    )
                if matches:
                    outputPathsByModel[modelName] = str(matches[0])
                else:
                    missingModels.append(modelName)

            if missingModels:
                foundList = "\n".join(f"  {f}" for f in allNiftiFiles) or "  (aucun fichier .nii.gz trouvé)"
                raise RuntimeError(
                    "Impossible d'associer les fichiers de sortie Moose aux modèles attendus : "
                    f"{', '.join(missingModels)}.\n"
                    f"Fichiers réellement produits par moosez :\n{foundList}\n"
                    "Le nommage des fichiers de sortie de moosez a probablement changé. "
                    "Inspectez la liste ci-dessus pour adapter le motif de correspondance."
                )

            predictionFile = self._simplifyMooseLabelMap(outputPathsByModel, subjectDirectory)
            self._loadLabelMapAsSegmentation(str(predictionFile), outputSegmentation)

        if showResult:
            outputSegmentation.CreateDefaultDisplayNodes()
            outputSegmentation.GetDisplayNode().SetVisibility(True)
            slicer.util.setSliceViewerLayers(background=inputVolumeCT)

        return outputSegmentation

    def process(
        self,
        inputVolumeCT: vtkMRMLScalarVolumeNode,
        outputSegmentation: vtkMRMLSegmentationNode,
        datasetId: int,
        configuration: str,
        trainer: str,
        plansIdentifier: str,
        checkpointName: str,
        foldsText: str,
        resultsFolder: str = "",
        disableTTA: bool = False,
        stepSize: float = 0.5,
        showResult: bool = True,
        modelName: str | None = None,
    ) -> vtkMRMLSegmentationNode:
        if not inputVolumeCT:
            raise ValueError("CT input volume is invalid")
        if datasetId <= 0:
            raise ValueError("Dataset ID must be greater than zero")
        if not outputSegmentation:
            raise ValueError("Output segmentation is invalid")

        if modelName == "ENHANCE-PET Moose Segmentation":
            return self._runMooseInference(inputVolumeCT, outputSegmentation, showResult=showResult)

        folds = self.parseFoldsText(foldsText)
        stepSizeValue = float(stepSize)
        if not (0.0 < stepSizeValue <= 1.0):
            raise ValueError("Step size must be in (0, 1]")

        resolvedResultsFolder = self._resolveResultsFolder(resultsFolder, datasetId=datasetId)
        if not resolvedResultsFolder:
            raise ValueError(
                "nnUNet results folder is not set. Provide a path or configure the nnUNet_results environment variable."
            )
        if not os.path.isdir(resolvedResultsFolder):
            raise FileNotFoundError(f"nnUNet results folder does not exist: {resolvedResultsFolder}")

        datasetPrefix = f"Dataset{int(datasetId):03d}_"
        if not any(path.is_dir() for path in Path(resolvedResultsFolder).glob(f"{datasetPrefix}*")):
            raise FileNotFoundError(
                f"nnUNet dataset folder {datasetPrefix}* was not found under results folder: {resolvedResultsFolder}"
            )

        caseName = self._sanitizeFileName(f"{inputVolumeCT.GetName()}_dataset{datasetId}")
        logging.info(
            "Starting nnUNetv2 organ inference for dataset %s, configuration %s, trainer %s, folds %s",
            datasetId,
            configuration,
            trainer,
            folds,
        )

        with tempfile.TemporaryDirectory(prefix="SegmentOrgansModule_") as tempDirectory:
            tempRoot = Path(tempDirectory)
            inputDirectory = tempRoot / "input"
            outputDirectory = tempRoot / "output"
            inputDirectory.mkdir()
            outputDirectory.mkdir()

            self._writeInputVolume(inputVolumeCT, inputDirectory, caseName)

            command = self._predictCommand()
            command.extend(
                [
                    "-i",
                    str(inputDirectory),
                    "-o",
                    str(outputDirectory),
                    "-d",
                    str(datasetId),
                    "-c",
                    configuration,
                    "-tr",
                    trainer,
                    "-p",
                    plansIdentifier,
                    "-f",
                    *folds,
                    "-step_size",
                    str(stepSizeValue),
                    "-chk",
                    checkpointName,
                ]
            )
            if disableTTA:
                command.append("--disable_tta")

            env = self._prepareSubprocessEnvironment(resolvedResultsFolder)
            self._runSubprocess(command, env=env, cwd=tempDirectory)

            predictionFile = self._findPredictionFile(outputDirectory, caseName)
            self._loadLabelMapAsSegmentation(str(predictionFile), outputSegmentation)

        if showResult:
            outputSegmentation.CreateDefaultDisplayNodes()
            outputSegmentation.GetDisplayNode().SetVisibility(True)
            slicer.util.setSliceViewerLayers(background=inputVolumeCT)

        return outputSegmentation

#
# SegmentOrgansModuleTest
#


class SegmentOrgansModuleTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def runTest(self):
        """Run as few or as many tests as needed here."""
        self.setUp()
        self.test_parse_folds()
        self.test_default_output_name()
        self.test_builtin_moose_model_smoke_with_phantom()

    def test_parse_folds(self):
        self.assertEqual(SegmentOrgansModuleLogic.parseFoldsText("0,1,2,3,4"), ["0", "1", "2", "3", "4"])
        self.assertEqual(SegmentOrgansModuleLogic.parseFoldsText("all"), ["all"])

    def test_default_output_name(self):
        logic = SegmentOrgansModuleLogic()
        volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "My Input Volume")
        self.assertEqual(logic.defaultOutputSegmentationName(volumeNode, "Organs"), "My_Input_Volume_Organs_organs")

    def test_builtin_moose_model_smoke_with_phantom(self):
        import numpy as np

        self.delayDisplay("Starting SegmentationOrgansModule smoke test")
        logic = SegmentOrgansModuleLogic()

        inputArray = np.zeros((16, 16, 16), dtype=np.float32)
        inputArray[3:11, 3:11, 3:11] = 1.0

        inputVolumeCT = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "MoosePhantomCT")
        slicer.util.updateVolumeFromArray(inputVolumeCT, inputArray)

        outputSegmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "MoosePhantomSegmentation")

        def fake_run_moose_inference(inputVolume, outputNode, showResult):
            labelMapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "MoosePhantomLabelMap")
            slicer.util.updateVolumeFromArray(labelMapNode, (slicer.util.arrayFromVolume(inputVolume) > 0).astype(np.uint8))
            slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelMapNode, outputNode)
            slicer.mrmlScene.RemoveNode(labelMapNode)
            return outputNode

        logic._runMooseInference = fake_run_moose_inference

        result = logic.process(
            inputVolumeCT=inputVolumeCT,
            outputSegmentation=outputSegmentation,
            datasetId=1,
            configuration="3d_fullres",
            trainer="nnUNetTrainer",
            plansIdentifier="nnUNetPlans",
            checkpointName="checkpoint_final.pth",
            foldsText="0",
            showResult=False,
            modelName="ENHANCE-PET Moose Segmentation",
        )

        self.assertIs(result, outputSegmentation)
        self.assertGreater(outputSegmentation.GetSegmentation().GetNumberOfSegments(), 0)
        self.delayDisplay("SegmentationOrgansModule smoke test passed")