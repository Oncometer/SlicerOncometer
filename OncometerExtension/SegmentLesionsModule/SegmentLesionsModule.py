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


@dataclass(frozen=True)
class NnUNetModelConfiguration:
    """Configuration used to run a custom nnUNetv2 model."""

    dataset_id: int
    configuration: str = "3d_fullres"
    trainer: str = "nnUNetTrainer"
    plans_identifier: str = "nnUNetPlans"
    checkpoint_name: str = "checkpoint_final.pth"
    folds: tuple[str, ...] = ("0", "1", "2", "3", "4")
    results_folder: str = ""


# Name of the built-in model used in "Simple" mode. Displayed to the user and
# used to build the default output segmentation name.
LION_MODEL_NAME = "ENHANCE PET Lion Segmentation"

#
# SegmentLesionsModule
#


class SegmentLesionsModule(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "1. Segmentation: lesions"
        self.parent.categories = ["Quantification.Oncometer"]
        self.parent.dependencies = []
        self.parent.contributors = [
            "Charlotte Boucherie (Centre Henri Becquerel)",
            "Solene Perret (Centre Henri Becquerel)",
            "Pierre Decazes (Centre Henri Becquerel)",
        ]
        self.parent.helpText = _("""
        Lesion segmentation using nnUNet with CT/PET scans.
        """)
        self.parent.acknowledgementText = _(
            """
            This file was originally developed by Charlotte Boucherie.
            """
        )


@parameterNodeWrapper
class SegmentLesionsModuleParameterNode:
    # Parameter node wrapper: module parameters observed to enable/disable the Apply button.
    # Parameters are automatically synchronized with widgets via connectGui in the widget.
    inputVolume: vtkMRMLScalarVolumeNode
    outputSegmentation: vtkMRMLSegmentationNode | None = None
    datasetId: Annotated[int, WithinRange(0, 99999)] = 0
    configuration: str = "3d_fullres"
    trainer: str = "nnUNetTrainer"
    plansIdentifier: str = "nnUNetPlans"
    checkpointName: str = "checkpoint_final.pth"
    folds: str = "0,1,2,3,4"
    resultsFolder: str = ""


class SegmentLesionsModuleWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    # Widget class: manages the user interface and starts the segmentation process
    # using the module logic. "Simple" mode always uses the built-in Lion model;
    # "Custom" mode lets the user run their own nnUNetv2 model.
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None
        self._modelSpacerItem = None
        self._lastExpandedModelHeight = 0

    def setup(self) -> None:
        # Setup called during widget initialization:
        # - load the UI from the .ui file
        # - create the module logic
        # - observe scene changes
        # - connect widget signals to handlers
        # - initialize the parameter node
        """Called when the user opens the module the first time and the widget is initialized."""
        super().setup()

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/SegmentLesionsModule.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)
        # Module logic that manages the segmentation process.
        self.logic = SegmentLesionsModuleLogic()

        # Observe scene changes to reset parameters when needed
        # (e.g., when a user loads a new scene, reset parameters to avoid references
        # to nodes that no longer exist)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        self.ui.modeComboBox.connect("currentIndexChanged(int)", self.onModeChanged)

        # Help buttons (optional, depends on .ui)
        modeHelpButton = getattr(self.ui, "modeHelpToolButton", None)
        if modeHelpButton is not None:
            modeHelpButton.connect("clicked(bool)", self.onModeHelpClicked)
        self._connectModelCollapsedSignal()
        if hasattr(self.ui, "resultsFolderBrowseButton"):
            self.ui.resultsFolderBrowseButton.connect("clicked(bool)", self.onBrowseResultsFolderButton)
        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)

        self._installModelSpacer()

        self.ui.modeComboBox.setCurrentIndex(0)
        self._updateModeUi()

        self.initializeParameterNode()

    def cleanup(self) -> None:
        self.removeObservers()

    def enter(self) -> None:
        self.initializeParameterNode()

    def exit(self) -> None:
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None
            if self.hasObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply):
                self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

    def onSceneStartClose(self, caller, event) -> None:
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        if self.parent.isEntered:
            self.initializeParameterNode()

    def _widgetStringValue(self, widget, attributeName: str) -> str:
        attribute = getattr(widget, attributeName)
        return attribute() if callable(attribute) else attribute

    def _widgetIntValue(self, widget, attributeName: str) -> int:
        attribute = getattr(widget, attributeName)
        return attribute() if callable(attribute) else attribute

    def _widgetBoolValue(self, widget, attributeName: str) -> bool:
        attribute = getattr(widget, attributeName, None)
        if attribute is None:
            return False
        try:
            return bool(attribute() if callable(attribute) else attribute)
        except Exception:
            return False

    def _widgetOptionalBoolValue(self, widget, attributeName: str) -> bool | None:
        attribute = getattr(widget, attributeName, None)
        if attribute is None:
            return None
        try:
            return bool(attribute() if callable(attribute) else attribute)
        except Exception:
            return None

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
        # Avoid compositor/placeholder artifacts when the UI thread blocks.
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
        busyBar.setRange(0, 0)  # indeterminate
        busyLayout.addWidget(busyLabel)
        busyLayout.addWidget(busyBar)
        busyDialog.setLayout(busyLayout)
        try:
            busyDialog.setMinimumSize(360, 120)
            busyDialog.resize(420, 140)
        except Exception:
            pass
        return busyDialog

    def _modeStackWidget(self):
        # Keep compatibility with older UI object names during iterative UI refactors.
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

    def _widgetHeight(self, widget) -> int:
        if widget is None:
            return 0
        sizeHintAttribute = getattr(widget, "sizeHint", None)
        sizeHintValue = sizeHintAttribute() if callable(sizeHintAttribute) else sizeHintAttribute
        sizeHintHeight = self._qtNumericAttribute(sizeHintValue, "height", 0) if sizeHintValue is not None else 0
        return max(
            self._qtNumericAttribute(widget, "height", 0),
            self._qtNumericAttribute(widget, "minimumHeight", 0),
            sizeHintHeight,
        )

    def _setSpacerHeight(self, spacer, height: int) -> None:
        if spacer is None:
            return
        spacer.changeSize(0, max(0, int(height)), qt.QSizePolicy.Minimum, qt.QSizePolicy.Fixed)
        if self.layout:
            self.layout.invalidate()

    def _installModelSpacer(self) -> None:
        if self._modelSpacerItem is not None:
            return
        self._modelSpacerItem = qt.QSpacerItem(0, 0, qt.QSizePolicy.Minimum, qt.QSizePolicy.Fixed)
        modelIndex = self.layout.indexOf(self.ui.modelCollapsibleButton)
        if modelIndex >= 0:
            self.layout.insertSpacerItem(modelIndex + 1, self._modelSpacerItem)

    def _connectModelCollapsedSignal(self) -> None:
        for signalName in ("contentsCollapsed(bool)", "collapsed(bool)", "toggled(bool)"):
            try:
                self.ui.modelCollapsibleButton.connect(signalName, self.onModelContentsCollapsed)
                return
            except Exception:
                continue

    def _updateModelReservedSpace(self) -> None:
        if self._modelSpacerItem is None:
            return

        isCollapsed = self._widgetOptionalBoolValue(self.ui.modelCollapsibleButton, "contentsCollapsed")
        if isCollapsed is None:
            isCollapsed = self._widgetOptionalBoolValue(self.ui.modelCollapsibleButton, "collapsed")
        if isCollapsed is None:
            checked = self._widgetOptionalBoolValue(self.ui.modelCollapsibleButton, "checked")
            isCollapsed = False if checked is None else (not checked)

        currentHeight = self._widgetHeight(self.ui.modelCollapsibleButton)
        if not isCollapsed:
            self._lastExpandedModelHeight = max(self._lastExpandedModelHeight, currentHeight)
            self._setSpacerHeight(self._modelSpacerItem, 0)
            return

        reserveHeight = max(0, self._lastExpandedModelHeight - currentHeight)
        self._setSpacerHeight(self._modelSpacerItem, reserveHeight)

    def _updateModeUi(self) -> None:
        modeIndex = self._widgetIntValue(self.ui.modeComboBox, "currentIndex")
        modeStackWidget = self._modeStackWidget()
        if not modeStackWidget:
            return
        modeStackWidget.setCurrentIndex(modeIndex)
        modeStackWidget.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Fixed)

        # Avoid large empty space by adjusting height to the active page
        currentPage = modeStackWidget.currentWidget()
        contentHeight = max(40, self._modePageContentHeight(currentPage))
        modeStackWidget.setMinimumHeight(contentHeight)
        modeStackWidget.setMaximumHeight(contentHeight)

        self.ui.modelCollapsibleButton.layout().invalidate()
        self.ui.modelCollapsibleButton.adjustSize()
        self._updateModelReservedSpace()

    def onModelContentsCollapsed(self, isCollapsed: bool) -> None:
        self._updateModelReservedSpace()

    def onModeChanged(self, index: int) -> None:
        self._updateModeUi()
        self._checkCanApply()

    def _isSimpleMode(self) -> bool:
        return self._widgetIntValue(self.ui.modeComboBox, "currentIndex") == 0

    # Open a folder selection dialog for results and apply the chosen folder to
    # the corresponding UI field
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
                "- The \"LION\" mode uses the built-in ENHANCE PET Lion Segmentation model.\n"
                "- To use your own nnUNetV2 model, select \"Custom\" mode and fill in the model parameters."
            ),
        )

    def initializeParameterNode(self) -> None:
        self.setParameterNode(self.logic.getParameterNode())

        if not self._parameterNode.inputVolume:
            firstVolumeNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
            if firstVolumeNode:
                self._parameterNode.inputVolume = firstVolumeNode

        self._checkCanApply()

    # Parameter node management: connect to Modified events to check whether Apply
    # can be activated, and disconnect when the parameter node changes or the
    # module is exited.
    def setParameterNode(self, inputParameterNode: SegmentLesionsModuleParameterNode | None) -> None:
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            if self.hasObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply):
                self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

        self._parameterNode = inputParameterNode

        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
            self._checkCanApply()

    # Check conditions to enable the Apply button. In "Simple" mode only a PET
    # volume is required (the built-in Lion model needs nothing else). In
    # "Custom" mode a dataset ID must also be provided.
    def _checkCanApply(self, caller=None, event=None) -> None:
        canApply = bool(self._parameterNode and self._parameterNode.inputVolume)
        if canApply and not self._isSimpleMode():
            canApply = self._parameterNode.datasetId > 0

        if canApply:
            self.ui.applyButton.toolTip = _("Run segmentation")
            self.ui.applyButton.enabled = True
        else:
            tooltip = _("Select a PET scan")
            if not self._isSimpleMode():
                tooltip = _("Select a PET scan and set the dataset ID for the custom model")
            self.ui.applyButton.toolTip = tooltip
            self.ui.applyButton.enabled = False

    # Get the custom model configuration from the UI fields, used only when
    # running in "Custom" mode.
    def _currentModelConfiguration(self) -> NnUNetModelConfiguration:
        return NnUNetModelConfiguration(
            dataset_id=self._widgetIntValue(self.ui.datasetIdSpinBox, "value"),
            configuration=self._widgetStringValue(self.ui.configurationComboBox, "currentText"),
            trainer=self._widgetStringValue(self.ui.trainerLineEdit, "text"),
            plans_identifier=self._widgetStringValue(self.ui.plansIdentifierLineEdit, "text"),
            checkpoint_name=self._widgetStringValue(self.ui.checkpointNameLineEdit, "text"),
            folds=tuple(self._parseFolds(self._widgetStringValue(self.ui.foldsLineEdit, "text"))),
            results_folder=self._widgetStringValue(self.ui.resultsFolderLineEdit, "text") if hasattr(self.ui, "resultsFolderLineEdit") else "",
        )

    def _parseFolds(self, foldsText: str) -> list[str]:
        return SegmentLesionsModuleLogic.parseFoldsText(foldsText)

    def onApplyButton(self) -> None:
        busyDialog = self._createBusyDialog(_("Lesion segmentation"), _("Segmentation in progress..."))
        try:
            busyDialog.show()
            busyDialog.raise_()
            busyDialog.activateWindow()
            busyDialog.repaint()
        except Exception:
            pass
        self._processEvents()

        def _run() -> None:
            completed = False
            outputSegmentation = self.ui.outputSegmentationSelector.currentNode()
            try:
                self.ui.applyButton.enabled = False
                with slicer.util.tryWithErrorDisplay(_("Failed to compute results."), waitCursor=True):
                    isSimpleMode = self._isSimpleMode()
                    modelName = LION_MODEL_NAME if isSimpleMode else _("Custom")

                    if not outputSegmentation:
                        outputName = self.logic.defaultOutputSegmentationName(
                            self.ui.inputSelector.currentNode(),
                            modelName,
                        )
                        outputSegmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", outputName)
                        self.ui.outputSegmentationSelector.setCurrentNode(outputSegmentation)

                    if isSimpleMode:
                        tracer = self._widgetStringValue(self.ui.tracerComboBox, "currentText").strip().lower()
                        self.logic.process(
                            inputVolumePET=self.ui.inputSelector.currentNode(),
                            outputSegmentation=outputSegmentation,
                            modelName=LION_MODEL_NAME,
                            tracer=tracer,
                            showResult=True,
                        )
                    else:
                        configuration = self._currentModelConfiguration()
                        self.logic.process(
                            inputVolumePET=self.ui.inputSelector.currentNode(),
                            outputSegmentation=outputSegmentation,
                            modelName=None,
                            datasetId=configuration.dataset_id,
                            configuration=configuration.configuration,
                            trainer=configuration.trainer,
                            plansIdentifier=configuration.plans_identifier,
                            checkpointName=configuration.checkpoint_name,
                            foldsText=self._widgetStringValue(self.ui.foldsLineEdit, "text"),
                            resultsFolder=configuration.results_folder,
                            disableTTA=False,
                            stepSize=0.5,
                            showResult=True,
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

        # Give Qt a moment to paint the dialog before heavy preprocessing starts.
        qt.QTimer.singleShot(100, _run)

#
# SegmentLesionsModuleLogic
#


class SegmentLesionsModuleLogic(ScriptedLoadableModuleLogic):
    """Implements the segmentation pipeline.

    "Simple" mode runs the built-in ENHANCE PET Lion model (lionz) on a PET
    volume only. "Custom" mode runs an arbitrary nnUNetv2 model on a
    PET+CT pair, using the dataset ID and training parameters provided by
    the user.
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
        return SegmentLesionsModuleParameterNode(super().getParameterNode())

    def _sanitizeFileName(self, value: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
        return sanitized or "case"

    def defaultOutputSegmentationName(self, inputVolume: vtkMRMLScalarVolumeNode | None, presetName: str | None = None) -> str:
        inputName = inputVolume.GetName() if inputVolume else "Segmentation"
        presetSuffix = f"_{self._sanitizeFileName(presetName)}" if presetName else ""
        return f"{self._sanitizeFileName(inputName)}{presetSuffix}_segmentation"

    def _resolveResultsFolder(self, explicitResultsFolder: str, datasetId: int | None = None) -> str:
        expanded = os.path.expandvars(os.path.expanduser((explicitResultsFolder or "").strip()))
        candidates: list[Path] = []
        if expanded:
            candidates.append(Path(expanded))

        envResults = os.environ.get("nnUNet_results", "").strip()
        if envResults:
            candidates.append(Path(envResults))

        for candidate in candidates:
            normalized = self._normalizeResultsRoot(candidate, datasetId)
            if normalized and normalized.is_dir():
                return str(normalized)

        return ""

    def _normalizeResultsRoot(self, root: Path, datasetId: int | None) -> Path | None:
        if not root or not root.is_dir():
            return None

        if datasetId is None:
            return root

        datasetPrefix = f"Dataset{int(datasetId):03d}_"

        # If user/config points directly at the dataset folder, accept it by using its parent.
        if root.name.startswith(datasetPrefix) and root.parent.is_dir():
            return root.parent

        # If the dataset folder exists under root, root is a valid nnUNet_results.
        try:
            if any(path.is_dir() for path in root.glob(f"{datasetPrefix}*")):
                return root
        except Exception:
            pass

        return root

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
                "Prediction failed.\n"
                f"Command: {' '.join(command)}\n"
                f"STDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
        return completed

    def _writeInputVolume(
        self,
        inputVolumePET: vtkMRMLScalarVolumeNode,
        inputDirectory: Path,
        caseName: str,
    ) -> Path:
        inputFilePET = inputDirectory / f"{caseName}_0000.nii.gz"

        if not slicer.util.saveNode(inputVolumePET, str(inputFilePET)):
            raise RuntimeError(f"Unable to save PET input volume to {inputFilePET}")

        return inputFilePET

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

    def _loadLabelMapAsSegmentation(self, labelMapPath: str, outputSegmentation: vtkMRMLSegmentationNode) -> None:
        labelMapNode = slicer.util.loadLabelVolume(labelMapPath)
        if not labelMapNode:
            raise RuntimeError(f"Unable to load predicted label map: {labelMapPath}")

        segmentation = outputSegmentation.GetSegmentation()
        while segmentation.GetNumberOfSegments() > 0:
            segmentation.RemoveSegment(segmentation.GetNthSegmentID(0))

        outputSegmentation.CreateDefaultDisplayNodes()
        slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelMapNode, outputSegmentation)
        outputSegmentation.CreateClosedSurfaceRepresentation()

        if outputSegmentation.GetSegmentation().GetNumberOfSegments() > 0:
            segmentId = outputSegmentation.GetSegmentation().GetNthSegmentID(0)
            outputSegmentation.GetSegmentation().GetSegment(segmentId).SetName("Lesion")
            outputSegmentation.GetDisplayNode().SetVisibility(True)

        slicer.mrmlScene.RemoveNode(labelMapNode)

    def _findLionPredictionFile(self, subjectDirectory: Path) -> Path:
        candidates = sorted(subjectDirectory.rglob("*_tumor_seg.nii.gz"))
        if candidates:
            return candidates[0]
        candidates = sorted(subjectDirectory.rglob("*.nii.gz"))
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise FileNotFoundError(f"No Lion prediction file found in {subjectDirectory}")
        raise FileNotFoundError(f"Multiple Lion prediction files found in {subjectDirectory}")

    def _runLionInference(
        self,
        inputVolumePET: vtkMRMLScalarVolumeNode,
        outputSegmentation: vtkMRMLSegmentationNode,
        tracer: str,
        showResult: bool,
    ) -> vtkMRMLSegmentationNode:
        # Run Lion (lionz) as a subprocess instead of importing/calling it in-process.
        caseName = self._sanitizeFileName(inputVolumePET.GetName() or "PT_scan")

        with tempfile.TemporaryDirectory(prefix="SegmentLesionsModule_Lion_") as tempDirectory:
            tempRoot = Path(tempDirectory)
            # lionz CLI expects: <main_directory>/<subject>/PT_*.nii.gz
            subjectDirectory = tempRoot / caseName
            subjectDirectory.mkdir(parents=True)

            inputFile = subjectDirectory / f"PT_{caseName}.nii.gz"
            if not slicer.util.saveNode(inputVolumePET, str(inputFile)):
                raise RuntimeError(f"Unable to save PET input volume to {inputFile}")

            # Script file rather than "-c": more robust with the PythonSlicer
            # launcher (avoids argument parsing/escaping issues seen with "-c").
            runnerScript = tempRoot / "_run_lionz.py"
            runnerScript.write_text(
                "import multiprocessing\n"
                "import sys\n"
                "from lionz.lionz import main\n"
                "if __name__ == '__main__':\n"
                "   multiprocessing.freeze_support()\n"
                "   sys.exit(main())\n"
            )

            command = [sys.executable, str(runnerScript), "-d", str(tempRoot), "-m", tracer]

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["LANG"] = "C.UTF-8"
            env["LC_ALL"] = "C.UTF-8"
            self._runSubprocess(command, env=env, cwd=str(tempRoot))

            predictionFile = self._findLionPredictionFile(subjectDirectory)
            self._loadLabelMapAsSegmentation(str(predictionFile), outputSegmentation)

        if showResult:
            outputSegmentation.CreateClosedSurfaceRepresentation()
            outputSegmentation.GetDisplayNode().SetVisibility(True)
            slicer.util.setSliceViewerLayers(background=inputVolumePET, foregroundOpacity=0.0)

        return outputSegmentation

    def process(
        self,
        inputVolumePET: vtkMRMLScalarVolumeNode,
        outputSegmentation: vtkMRMLSegmentationNode,
        modelName: str | None = LION_MODEL_NAME,
        tracer: str = "fdg",
        datasetId: int = 0,
        configuration: str = "3d_fullres",
        trainer: str = "nnUNetTrainer",
        plansIdentifier: str = "nnUNetPlans",
        checkpointName: str = "checkpoint_final.pth",
        foldsText: str = "0,1,2,3,4",
        resultsFolder: str = "",
        disableTTA: bool = False,
        stepSize: float = 0.5,
        showResult: bool = True,
    ) -> vtkMRMLSegmentationNode:
        if not inputVolumePET:
            raise ValueError("PET input volume is invalid")
        if not outputSegmentation:
            raise ValueError("Output segmentation is invalid")

        if modelName == LION_MODEL_NAME:
            return self._runLionInference(inputVolumePET, outputSegmentation, tracer=tracer, showResult=showResult)

        # Custom nnUNetv2 model: PET only, driven by dataset ID and training parameters.
        if datasetId <= 0:
            raise ValueError("Dataset ID must be greater than zero")

        folds = self.parseFoldsText(foldsText)
        stepSizeValue = float(stepSize)
        if not (0.0 < stepSizeValue <= 1.0):
            raise ValueError("Step size must be in (0, 1]")
        resolvedResultsFolder = self._resolveResultsFolder(resultsFolder, datasetId=datasetId)
        if not resolvedResultsFolder:
            raise ValueError("nnUNet results folder is not set. Provide a path or configure the nnUNet_results environment variable.")
        if not os.path.isdir(resolvedResultsFolder):
            raise FileNotFoundError(f"nnUNet results folder does not exist: {resolvedResultsFolder}")

        datasetPrefix = f"Dataset{int(datasetId):03d}_"
        if not any(path.is_dir() for path in Path(resolvedResultsFolder).glob(f"{datasetPrefix}*")):
            raise FileNotFoundError(
                f"nnUNet dataset folder {datasetPrefix}* was not found under results folder: {resolvedResultsFolder}"
            )

        caseName = self._sanitizeFileName(f"{inputVolumePET.GetName()}_dataset{datasetId}")
        logging.info(
            "Starting nnUNetv2 inference for dataset %s, configuration %s, trainer %s, folds %s",
            datasetId,
            configuration,
            trainer,
            folds,
        )

        with tempfile.TemporaryDirectory(prefix="SegmentLesionsModule_") as tempDirectory:
            tempRoot = Path(tempDirectory)
            inputDirectory = tempRoot / "input"
            outputDirectory = tempRoot / "output"
            inputDirectory.mkdir()
            outputDirectory.mkdir()

            self._writeInputVolume(inputVolumePET, inputDirectory, caseName)

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
            outputSegmentation.CreateClosedSurfaceRepresentation()
            outputSegmentation.GetDisplayNode().SetVisibility(True)
            slicer.util.setSliceViewerLayers(background=inputVolumePET, foregroundOpacity=0.0)

        return outputSegmentation


class SegmentLesionsModuleTest(ScriptedLoadableModuleTest):
    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_parse_folds()
        self.test_default_output_name()
        self.test_builtin_lion_model_smoke_with_phantom()

    def test_parse_folds(self):
        self.assertEqual(SegmentLesionsModuleLogic.parseFoldsText("0,1,2,3,4"), ["0", "1", "2", "3", "4"])
        self.assertEqual(SegmentLesionsModuleLogic.parseFoldsText("all"), ["all"])

    def test_default_output_name(self):
        logic = SegmentLesionsModuleLogic()
        volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "My Input Volume")
        self.assertEqual(
            logic.defaultOutputSegmentationName(volumeNode, LION_MODEL_NAME),
            "My_Input_Volume_ENHANCE_PET_Lion_Segmentation_segmentation",
        )

    def test_builtin_lion_model_smoke_with_phantom(self):
        import numpy as np

        self.delayDisplay("Starting SegmentationLesionsModule smoke test")
        logic = SegmentLesionsModuleLogic()

        inputArray = np.zeros((16, 16, 16), dtype=np.float32)
        inputArray[4:12, 4:12, 4:12] = 1.0

        inputVolumePET = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "LionPhantomPET")
        slicer.util.updateVolumeFromArray(inputVolumePET, inputArray)

        outputSegmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "LionPhantomSegmentation")

        def fake_run_lion_inference(inputVolume, outputNode, tracer, showResult):
            labelMapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "LionPhantomLabelMap")
            slicer.util.updateVolumeFromArray(labelMapNode, (slicer.util.arrayFromVolume(inputVolume) > 0).astype(np.uint8))
            slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelMapNode, outputNode)
            slicer.mrmlScene.RemoveNode(labelMapNode)
            return outputNode

        logic._runLionInference = fake_run_lion_inference

        result = logic.process(
            inputVolumePET=inputVolumePET,
            outputSegmentation=outputSegmentation,
            modelName=LION_MODEL_NAME,
            showResult=False,
        )

        self.assertIs(result, outputSegmentation)
        self.assertGreater(outputSegmentation.GetSegmentation().GetNumberOfSegments(), 0)
        self.delayDisplay("SegmentationLesionsModule smoke test passed")