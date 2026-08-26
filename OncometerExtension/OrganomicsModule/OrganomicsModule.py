import logging
import math
import os
from typing import Annotated, Callable
import re

import qt
import vtk
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import (
    parameterNodeWrapper,
    WithinRange,
)

from slicer import vtkMRMLScalarVolumeNode, vtkMRMLSegmentationNode, vtkMRMLTableNode


#
# OrganomicsModule
#


class OrganomicsModule(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "2. Extraction: organomics"
        self.parent.categories = ["Quantification.Oncometer"]
        self.parent.dependencies = []
        self.parent.contributors =[
            "Charlotte Boucherie (Centre Henri Becquerel)",
            "Solene Perret (Centre Henri Becquerel)",
            "Pierre Decazes (Centre Henri Becquerel)",] 
        self.parent.helpText = _("""
        Organomics extraction on PET Scans
        
        Inputs:
        - Segmentation volume with labels: 1=Spleen, 2=Kidneys, 3=Liver, 4=Pancreas, 5=Adrenal Glands, 6=Heart, 7=Brain, 8=Lungs, 9=Bones
        - PET volume
        
        """)
        self.parent.acknowledgementText = _(
            """
            This file was originally developed by Charlotte Boucherie.
            """
        )

#
# OrganomicsModuleParameterNode
#


@parameterNodeWrapper
class OrganomicsModuleParameterNode:
    """
    The parameters needed by module.

    segmentationVolume - A segmentation node (vtkMRMLSegmentationNode).
    petVolume - The PET scan volume.
    outputTable - Optional output table node to store results.
    """

    segmentationVolume: vtkMRMLSegmentationNode
    petVolume: vtkMRMLScalarVolumeNode
    outputTable: vtkMRMLTableNode | None = None


#
# OrganomicsModuleWidget
#


class OrganomicsModuleWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OrganomicsModule.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # The .ui file currently connects mrmlSceneChanged only for some selectors.
        # If a selector has no MRML scene then it stays disabled (greyed out).
        if hasattr(self.ui, "segmentationSelector"):
            self.ui.segmentationSelector.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OrganomicsModuleLogic()

        # Connections

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Keep the output table list in sync with the scene
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.NodeAddedEvent, self._onSceneContentChanged)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.NodeRemovedEvent, self._onSceneContentChanged)

        # Buttons
        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)
        # Organ selection buttons
        try:
            self.ui.pushButton_select_all_organs.connect("clicked(bool)", self.selectAllOrgans)
            self.ui.pushButton_deselect_all_organs.connect("clicked(bool)", self.deselectAllOrgans)
        except Exception:
            pass
        # Organomics selection buttons
        try:
            self.ui.pushButton_select_all_organomics.connect("clicked(bool)", self.selectAllOrganomics)
            self.ui.pushButton_deselect_all_organomics.connect("clicked(bool)", self.deselectAllOrganomics)
        except Exception:
            pass

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        self._refreshComboBoxes()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        self._refreshComboBoxes()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        # Do not react to parameter node changes (GUI will be updated when the user enters into the module)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None
            self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)
        if hasattr(self.ui, "comboBox_outputTable"):
            self.ui.comboBox_outputTable.clear()

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        # If this module is shown while the scene is closed then recreate a new parameter node immediately
        if self.parent.isEntered:
            self.initializeParameterNode()
        self._refreshComboBoxes()

    def _onSceneContentChanged(self, caller=None, event=None) -> None:
        self._refreshComboBoxes()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

        # Select default input nodes if nothing is selected yet to save a few clicks for the user
        if not self._parameterNode.segmentationVolume:
            # Segmentation node
            firstSegmentationNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLSegmentationNode")
            if firstSegmentationNode:
                self._parameterNode.segmentationVolume = firstSegmentationNode
        
        
        if not self._parameterNode.petVolume:
            # Try to find a second volume node for PET
            volumeNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
            if volumeNode and volumeNode != self._parameterNode.segmentationVolume:
                self._parameterNode.petVolume = volumeNode

    def setParameterNode(self, inputParameterNode: OrganomicsModuleParameterNode | None) -> None:
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then the GUI must be updated immediately.
        """

        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            # Note: in the .ui file, a Qt dynamic property called "SlicerParameterName" is set on each
            # ui element that needs connection.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
            self._checkCanApply()
            self._refreshComboBoxes()

    @staticmethod
    def _comboCurrentData(comboBox):
        getter = getattr(comboBox, "currentData", None)
        if getter is None:
            return None
        return getter() if callable(getter) else getter

    @staticmethod
    def _comboCount(comboBox) -> int:
        getter = getattr(comboBox, "count", None)
        return int(getter() if callable(getter) else getter)

    def _selectedComboNode(self, comboBox):
        nodeId = self._comboCurrentData(comboBox)
        if not nodeId or nodeId == "__NEW__":
            return None
        return slicer.mrmlScene.GetNodeByID(str(nodeId))

    def _refreshComboBoxWithNodes(self, comboBox, classNames: list[str], includeCreateNew: bool = False, createLabel: str = "") -> None:
        currentNode = self._selectedComboNode(comboBox)
        comboBox.clear()

        if includeCreateNew:
            comboBox.addItem(createLabel, "__NEW__")

        for className in classNames:
            nodes = slicer.util.getNodesByClass(className)
            for node in nodes:
                comboBox.addItem(node.GetName(), node.GetID())

        if currentNode:
            for index in range(self._comboCount(comboBox)):
                if comboBox.itemData(index) == currentNode.GetID():
                    comboBox.setCurrentIndex(index)
                    break

    def _refreshComboBoxes(self) -> None:
        if hasattr(self.ui, "comboBox_outputTable"):
            self._refreshComboBoxWithNodes(
                self.ui.comboBox_outputTable,
                ["vtkMRMLTableNode"],
                includeCreateNew=True,
                createLabel="Nouvelle table",
            )

    def _resolveOutputTableNode(self, segmentationNode) -> vtkMRMLTableNode:
        tableNode = None
        if hasattr(self.ui, "comboBox_outputTable"):
            tableNode = self._selectedComboNode(self.ui.comboBox_outputTable)
        if tableNode and tableNode.IsA("vtkMRMLTableNode"):
            if self._parameterNode is not None:
                self._parameterNode.outputTable = tableNode
            return tableNode

        segmentationName = segmentationNode.GetName() if segmentationNode else "Seg"
        tableName = f"Organomics_{segmentationName}"
        tableNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", tableName)

        if self._parameterNode is not None:
            self._parameterNode.outputTable = tableNode

        self._refreshComboBoxes()
        if hasattr(self.ui, "comboBox_outputTable"):
            for index in range(self._comboCount(self.ui.comboBox_outputTable)):
                if self.ui.comboBox_outputTable.itemData(index) == tableNode.GetID():
                    self.ui.comboBox_outputTable.setCurrentIndex(index)
                    break
        return tableNode

    def _checkCanApply(self, caller=None, event=None) -> None:
        if self._parameterNode and self._parameterNode.segmentationVolume and self._parameterNode.petVolume:
            self.ui.applyButton.toolTip = _("Extract organomics features")
            self.ui.applyButton.enabled = True
        else:
            self.ui.applyButton.toolTip = _("Select segmentation and PET volumes")
            self.ui.applyButton.enabled = False

    def onApplyButton(self) -> None:
        """Run processing when user clicks "Apply" button."""
        with slicer.util.tryWithErrorDisplay(_("Failed to compute results."), waitCursor=True):
            progressDialog = slicer.util.createProgressDialog(
                value=0,
                maximum=0,  # indeterminate / busy indicator
                labelText=_("Initialisation..."),
                windowTitle=_("Extraction organomique"),
                showCancelButton=True,
            )
            progressDialog.setMinimumDuration(0)
            outputTableNode = None

            def reportProgress(message: str) -> None:
                if progressDialog.wasCanceled:
                    raise RuntimeError(_("Extraction annulée."))
                progressDialog.setLabelText(str(message))
                try:
                    slicer.app.processEvents()
                except Exception:
                    qt.QCoreApplication.processEvents()

            try:
                # Best-effort modality across Slicer / Qt bindings
                try:
                    progressDialog.setWindowModality(qt.Qt.WindowModal)
                except Exception:
                    pass

                # Read input nodes
                reportProgress(_("Lecture des entrées..."))
                pet_node = self.ui.inputSelector.currentNode()
                seg_node = None
                if hasattr(self.ui, 'segmentationSelector'):
                    seg_node = self.ui.segmentationSelector.currentNode()
                elif hasattr(self.ui, 'comboBox'):
                    # fallback: try to read comboBox text and find node by name
                    try:
                        nodeName = self.ui.comboBox.currentText
                        seg_node = slicer.util.getNode(nodeName)
                    except Exception:
                        seg_node = None

                # Compute organomics features
                reportProgress(_("Calcul des mesures organomiques..."))
                results = self.logic.process(seg_node, pet_node, progressCallback=reportProgress)

                # Resolve output table node (Radiomics-like)
                reportProgress(_("Préparation de la table de sortie..."))
                outputTableNode = self._resolveOutputTableNode(seg_node)

                # Gather selected organs from checkboxes
                selected_labels = []
                try:
                    if self.ui.checkBox.checked:
                        selected_labels.append(1)
                    if self.ui.checkBox_4.checked:
                        selected_labels.append(2)
                    if self.ui.checkBox_3.checked:
                        selected_labels.append(3)
                    if self.ui.checkBox_2.checked:
                        selected_labels.append(4)
                    if self.ui.checkBox_9.checked:
                        selected_labels.append(5)
                    if self.ui.checkBox_8.checked:
                        selected_labels.append(6)
                    if self.ui.checkBox_7.checked:
                        selected_labels.append(7)
                    if self.ui.checkBox_6.checked:
                        selected_labels.append(8)
                    if self.ui.checkBox_5.checked:
                        selected_labels.append(9)
                except Exception:
                    # if any checkbox missing, default to all labels
                    selected_labels = []

                if not selected_labels:
                    selected_labels = [l for l in sorted(self.logic.LABEL_MAP.keys()) if l > 0]

                # Gather selected organomics measures
                selected_measures = []
                try:
                    if self.ui.checkBox_10.checked:
                        selected_measures.append('SUV_mean')
                    if self.ui.checkBox_11.checked:
                        selected_measures.append('SUV_max')
                except Exception:
                    selected_measures = []

                if not selected_measures:
                    selected_measures = ['SUV_mean', 'SUV_max']

                # Create results table with filters
                if results:
                    reportProgress(_("Écriture des résultats dans la table..."))
                    self.logic.resultsToTable(
                        outputTableNode,
                        results,
                        selected_labels=selected_labels,
                        selected_measures=selected_measures,
                    )

                reportProgress(_("Terminé."))
            finally:
                try:
                    progressDialog.close()
                except Exception:
                    pass

            if outputTableNode is not None:
                title = _("Calcul terminé")
                message = _("Extraction organomique terminée.\nTable : {tableName}").format(
                    tableName=outputTableNode.GetName() if outputTableNode else ""
                )
                try:
                    qt.QMessageBox.information(slicer.util.mainWindow(), title, message)
                except Exception:
                    slicer.util.showStatusMessage(message, 8000)

            return

    def selectAllOrgans(self, checked: bool = True) -> None:
        try:
            for attr in ['checkBox','checkBox_2','checkBox_3','checkBox_4','checkBox_5','checkBox_6','checkBox_7','checkBox_8','checkBox_9']:
                if hasattr(self.ui, attr):
                    getattr(self.ui, attr).setChecked(True)
        except Exception:
            pass

    def deselectAllOrgans(self, checked: bool = False) -> None:
        try:
            for attr in ['checkBox','checkBox_2','checkBox_3','checkBox_4','checkBox_5','checkBox_6','checkBox_7','checkBox_8','checkBox_9']:
                if hasattr(self.ui, attr):
                    getattr(self.ui, attr).setChecked(False)
        except Exception:
            pass

    def selectAllOrganomics(self, checked: bool = True) -> None:
        try:
            for attr in ['checkBox_10','checkBox_11']:
                if hasattr(self.ui, attr):
                    getattr(self.ui, attr).setChecked(True)
        except Exception:
            pass

    def deselectAllOrganomics(self, checked: bool = False) -> None:
        try:
            for attr in ['checkBox_10','checkBox_11']:
                if hasattr(self.ui, attr):
                    getattr(self.ui, attr).setChecked(False)
        except Exception:
            pass


#
# OrganomicsModuleLogic
#


class OrganomicsModuleLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    # Label mapping: label -> (name, description)
    LABEL_MAP = {
        0: {"name": "Background"},
        1: {"name": "Spleen"},
        2: {"name": "Kidneys"},
        3: {"name": "Liver"},
        4: {"name": "Pancreas"},
        5: {"name": "Adrenal Glands"},
        6: {"name": "Heart"},
        7: {"name": "Brain"},
        8: {"name": "Lungs"},
        9: {"name": "Bones"},
    }

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OrganomicsModuleParameterNode(super().getParameterNode())

    def process(self,
                segmentationVolume: vtkMRMLSegmentationNode,
                petVolume: vtkMRMLScalarVolumeNode,
                progressCallback: Callable[[str], None] | None = None) -> dict:
        """
        Extract organomics features from PET scan using segmentation labels.
        
        :param segmentationVolume: segmentation volume with labels 0-9
        :param petVolume: PET scan volume
        :return: dictionary with SUV statistics for each label
        """

        if not segmentationVolume or not petVolume:
            raise ValueError("Segmentation volume or PET volume is invalid")

        def progress(message: str) -> None:
            if progressCallback is not None:
                progressCallback(message)

        import time
        import numpy as np

        startTime = time.time()
        logging.info("Organomics extraction started")

        progress(_("Lecture du volume PET..."))

        # Get data from volumes
        pet_data = slicer.util.arrayFromVolume(petVolume)

        progress(_("Association des segments aux labels..."))

        import re

        segmentation = segmentationVolume.GetSegmentation()
        segmentIds = vtk.vtkStringArray()
        segmentation.GetSegmentIDs(segmentIds)

        # Association explicite label -> segmentId, basée sur le nom du segment (ex: "3" pour Liver).
        # On n'utilise PAS ExportVisibleSegmentsToLabelmapNode : cette fonction renumérote les
        # segments de façon séquentielle (1,2,3...) selon leur ordre, sans préserver les valeurs
        # de label d'origine. Ici, chaque segment est lu individuellement par son ID, ce qui
        # garantit une correspondance correcte quel que soit l'ordre ou les labels manquants.
        labelToSegmentId: dict[int, str] = {}
        for i in range(segmentIds.GetNumberOfValues()):
            segId = segmentIds.GetValue(i)
            segment = segmentation.GetSegment(segId)
            segmentName = segment.GetName() or ""
            match = re.search(r"(\d+)", segmentName)
            if match:
                labelToSegmentId[int(match.group(1))] = segId

        # Extract organomics for each label
        results = {}
        
        for label in sorted(self.LABEL_MAP.keys()):
            labelName = self.LABEL_MAP.get(label, {}).get("name", f"label_{label}")
            progress(_("Calcul SUV pour : {labelName}").format(labelName=labelName))

            if label == 0:
                continue  # Background : jamais calculé

            segId = labelToSegmentId.get(label)
            if segId is None:
                results[label] = {
                    "name": self.LABEL_MAP[label]["name"],
                    "SUV_mean": 0.0,
                    "SUV_max": 0.0,
                    "SUV_min": 0.0,
                    "n_voxels": 0,
                    "volume_mm3": 0.0,
                }
                continue

            # arrayFromSegmentBinaryLabelmap rééchantillonne déjà sur la géométrie de petVolume :
            # plus besoin de bricolage de transposition pour shape mismatch.
            segmentArray = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationVolume, segId, petVolume)
            mask = segmentArray > 0

            if np.any(mask):
                values = pet_data[mask]
                results[label] = {
                    "name": self.LABEL_MAP[label]["name"],
                    "SUV_mean": float(np.mean(values)),
                    "SUV_max": float(np.max(values)),
                    "SUV_min": float(np.min(values)),
                    "n_voxels": int(np.sum(mask)),
                    "volume_mm3": float(np.sum(mask)),
                }
                logging.info(f"{self.LABEL_MAP[label]['name']}: SUV_mean={results[label]['SUV_mean']:.3f}, "
                        f"SUV_max={results[label]['SUV_max']:.3f}, n_voxels={results[label]['n_voxels']}")
            else:
                results[label] = {
                    "name": self.LABEL_MAP[label]["name"],
                    "SUV_mean": 0.0,
                    "SUV_max": 0.0,
                    "SUV_min": 0.0,
                    "n_voxels": 0,
                    "volume_mm3": 0.0,
                }

        stopTime = time.time()
        logging.info(f"Organomics extraction completed in {stopTime-startTime:.2f} seconds")

        progress(_("Organomics : terminé."))
        
        return results

    @staticmethod
    def _rowsToTable(outputTableNode: vtkMRMLTableNode, rows: list[dict]) -> None:
        table = outputTableNode.GetTable()
        table.Initialize()
        if not rows:
            outputTableNode.Modified()
            return

        columns = []
        for row in rows:
            for key in row.keys():
                if key not in columns:
                    columns.append(key)

        for columnName in columns:
            column = vtk.vtkStringArray()
            column.SetName(columnName)
            table.AddColumn(column)

        table.SetNumberOfRows(len(rows))
        for rowIndex, row in enumerate(rows):
            for columnIndex, columnName in enumerate(columns):
                value = row.get(columnName, "")
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    value = ""
                table.SetValue(rowIndex, columnIndex, str(value))

        outputTableNode.Modified()

    def resultsToTable(
        self,
        outputTableNode: vtkMRMLTableNode,
        results: dict,
        selected_labels: list | None = None,
        selected_measures: list | None = None,
    ) -> None:
        """
        Convert results dictionary to a MRML table node for display.

        :param outputTableNode: MRML table node to fill/update
        :param results: dictionary with organomics results
        :param selected_labels: list of integer labels to include (None means all >0)
        :param selected_measures: list of measure keys to include, e.g. ['SUV_mean','SUV_max']
        :return: None (table is updated in-place)
        """
        if outputTableNode is None:
            raise ValueError("Table de sortie invalide")
        if selected_labels is None:
            selected_labels = [l for l in sorted(self.LABEL_MAP.keys()) if l > 0]
        if selected_measures is None:
            selected_measures = ['SUV_mean', 'SUV_max']

        rows = []
        for label in selected_labels:
            result = results.get(label)
            if not result:
                continue
            row = {
                "organ": result.get("name", f"label_{label}"),
                "label": int(label),
            }
            for measure in selected_measures:
                row[measure] = result.get(measure, "")
            row["n_voxels"] = result.get("n_voxels", "")
            rows.append(row)

        if not rows:
            rows = [{"organ": "", "label": ""}]

        self._rowsToTable(outputTableNode, rows)


#
# OrganomicsModuleTest
#


class OrganomicsModuleTest(ScriptedLoadableModuleTest):
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
        self.test_OrganomicsModule1()

    def test_OrganomicsModule1(self):
        """Ideally you should have several levels of tests.  At the lowest level
        tests should exercise the functionality of the logic with different inputs
        (both valid and invalid).  At higher levels your tests should emulate the
        way the user would interact with your code and confirm that it still works
        the way you intended.
        One of the most important features of the tests is that it should alert other
        developers when their changes will have an impact on the behavior of your
        module.  For example, if a developer removes a feature that you depend on,
        your test should break so they know that the feature is needed.
        """

        self.delayDisplay("Starting OrganomicsModule smoke test")
        logic = OrganomicsModuleLogic()
        self.assertIsNotNone(logic)

        import numpy as np

        petArray = np.zeros((16, 16, 16), dtype=np.float32)
        petArray[2:6, 2:6, 2:6] = 5.0
        petArray[9:13, 9:13, 9:13] = 12.0

        segmentationArray = np.zeros((16, 16, 16), dtype=np.uint8)
        segmentationArray[2:6, 2:6, 2:6] = 1
        segmentationArray[9:13, 9:13, 9:13] = 3

        petVolumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "OrganomicsPhantomPET")
        slicer.util.updateVolumeFromArray(petVolumeNode, petArray)

        labelMapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "OrganomicsPhantomLabelMap")
        slicer.util.updateVolumeFromArray(labelMapNode, segmentationArray)

        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "OrganomicsPhantomSegmentation")
        slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelMapNode, segmentationNode)

        results = logic.process(segmentationNode, petVolumeNode)

        self.assertIn(1, results)
        self.assertIn(3, results)
        self.assertAlmostEqual(results[1]["SUV_mean"], 5.0)
        self.assertAlmostEqual(results[3]["SUV_max"], 12.0)
        self.assertGreater(results[1]["n_voxels"], 0)
        self.delayDisplay("OrganomicsModule smoke test passed")