import itertools
import importlib.util
import logging
import math
import os
from typing import Callable
import numpy as np
import qt
import SimpleITK as sitk
import sitkUtils
import vtk

import slicer
from slicer import vtkMRMLLabelMapVolumeNode, vtkMRMLScalarVolumeNode, vtkMRMLSegmentationNode, vtkMRMLTableNode
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleTest
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleWidget
from slicer.util import VTKObservationMixin


def _ensurePythonPackage(packageName: str, moduleName: str) -> None:
    if importlib.util.find_spec(moduleName) is not None:
        return

    try:
        slicer.util.pip_install(packageName)
    except Exception as error:
        raise RuntimeError(
            f"Unable to install the Python package '{packageName}' in Slicer's environment."
        ) from error

    if importlib.util.find_spec(moduleName) is None:
        raise RuntimeError(
            f"The Python package '{packageName}' was installed but is not available yet. "
            "Restart Slicer and try again."
        )


#
# RadiomicsModule
#


class RadiomicsModule(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "2. Extraction: radiomics"
        self.parent.categories = ["Quantification.Oncometer"]
        self.parent.dependencies = []
        self.parent.contributors =[
            "Charlotte Boucherie (Centre Henri Becquerel)",
            "Solene Perret (Centre Henri Becquerel)",
            "Pierre Decazes (Centre Henri Becquerel)",] 
        self.parent.helpText = _("""
        Radiomics extraction from PET volumes and lesion segmentation masks.
        """)
        self.parent.acknowledgementText = _(
            """
            This file was originally developed by Charlotte Boucherie.
            """
        )


#
# RadiomicsModuleWidget
#


class RadiomicsModuleWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._metricCheckBoxes = []
        self._globalMetricCheckBoxes = []
        self._localMetricCheckBoxes = []

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/RadiomicsModule.ui"))
        self.layout.addWidget(uiWidget)
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = RadiomicsModuleLogic()

        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)
        self.ui.inputSelector.connect("currentNodeChanged(vtkMRMLNode*)", self._onSceneContentChanged)

        self._metricCheckBoxes = [
            self.ui.checkBox_nROI,
            self.ui.checkBox_Dmax,
            self.ui.checkBox_SUVmean,
            self.ui.checkBox_SUVmax,
            self.ui.checkBox_TMTV,
            self.ui.checkBox_TLG,
            self.ui.checkBox_TMTS,
            self.ui.checkBox_SurfaceTumorale_mm2,
            self.ui.checkBox_VolumeTumoral_mm3,
            self.ui.checkBox_FirstOrder,
            self.ui.checkBox_medPCD,
            self.ui.checkBox_90PCD,
            self.ui.checkBox_medEdgePCD,
            self.ui.checkBox_TVSR,
            self.ui.checkBox_TumBB,
            self.ui.checkBox_NHOC,
            self.ui.checkBox_NHOP,
            self.ui.checkBox_sdSphericity
        ]
        for checkBox in self._metricCheckBoxes:
            checkBox.connect("toggled(bool)", self._checkCanApply)

        helpButton = self._findUiButton("pushButton_help")
        if helpButton is not None:
            helpButton.connect("clicked(bool)", self._onHelpClicked)

        self._connectSelectionButtons()

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.NodeAddedEvent, self._onSceneContentChanged)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.NodeRemovedEvent, self._onSceneContentChanged)

        self._refreshComboBoxes()
        self._checkCanApply()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        self._refreshComboBoxes()
        self._checkCanApply()

    def exit(self) -> None:
        pass

    def onSceneStartClose(self, caller, event) -> None:
        self.ui.comboBox_inputSegmentation.clear()
        self.ui.comboBox.clear()
        self._checkCanApply()

    def onSceneEndClose(self, caller, event) -> None:
        self._refreshComboBoxes()
        self._checkCanApply()

    def _onSceneContentChanged(self, caller=None, event=None) -> None:
        self._refreshComboBoxes()
        self._checkCanApply()

    def _setCheckBoxes(self, checkBoxes, checked: bool) -> None:
        for checkBox in checkBoxes:
            checkBox.setChecked(bool(checked))

    def _onSelectAllClicked(self, checked=False) -> None:
        self._setCheckBoxes(self._metricCheckBoxes, True)

    def _onDeselectAllClicked(self, checked=False) -> None:
        self._setCheckBoxes(self._metricCheckBoxes, False)

    def _connectSelectionButtons(self) -> None:
        selectButton = self._findUiButton("pushButton_select_all")
        deselectButton = self._findUiButton("pushButton_deselect_all")

        if selectButton:
            selectButton.connect("clicked(bool)", self._onSelectAllClicked)
        if deselectButton:
            deselectButton.connect("clicked(bool)", self._onDeselectAllClicked)

    def _findUiButton(self, objectName: str):
        widget = getattr(self, "uiWidget", None)
        if widget is not None:
            button = widget.findChild(qt.QPushButton, objectName)
            if button is not None:
                return button
        return getattr(self.ui, objectName, None)

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

    @staticmethod
    def _isChecked(checkBox) -> bool:
        getter = getattr(checkBox, "isChecked", None)
        if callable(getter):
            return bool(getter())
        return bool(getattr(checkBox, "checked", False))

    def _selectedComboNode(self, comboBox):
        nodeId = self._comboCurrentData(comboBox)
        if not nodeId:
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
        self._refreshComboBoxWithNodes(
            self.ui.comboBox_inputSegmentation,
            ["vtkMRMLSegmentationNode", "vtkMRMLLabelMapVolumeNode"],
            includeCreateNew=False,
        )
        self._refreshComboBoxWithNodes(
            self.ui.comboBox,
            ["vtkMRMLTableNode"],
            includeCreateNew=True,
            createLabel="New table",
        )

    def _selectedMetrics(self) -> dict:
        return {
            "nROI": self._isChecked(self.ui.checkBox_nROI),
            "Dmax": self._isChecked(self.ui.checkBox_Dmax),
            "SUVmean": self._isChecked(self.ui.checkBox_SUVmean),
            "SUVmax": self._isChecked(self.ui.checkBox_SUVmax),
            "TMTV": self._isChecked(self.ui.checkBox_TMTV),
            "TLG": self._isChecked(self.ui.checkBox_TLG),
            "TMTS": self._isChecked(self.ui.checkBox_TMTS),
            "surface_tumorale_mm2": self._isChecked(self.ui.checkBox_SurfaceTumorale_mm2),
            "volume_tumoral_mm3": self._isChecked(self.ui.checkBox_VolumeTumoral_mm3),
            "FirstOrder": self._isChecked(self.ui.checkBox_FirstOrder),
            "medPCD": self._isChecked(self.ui.checkBox_medPCD),
            "90thPCD": self._isChecked(self.ui.checkBox_90PCD),
            "medEdgePCD": self._isChecked(self.ui.checkBox_medEdgePCD),
            "TVSR": self._isChecked(self.ui.checkBox_TVSR),
            "TumBB": self._isChecked(self.ui.checkBox_TumBB),
            "NHOC": self._isChecked(self.ui.checkBox_NHOC),
            "NHOP": self._isChecked(self.ui.checkBox_NHOP),
            "sdSphericity": self._isChecked(self.ui.checkBox_sdSphericity)
        }

    def _resolveOutputTableNode(self, inputVolumeNode, segmentationNode) -> vtkMRMLTableNode:
        tableNode = self._selectedComboNode(self.ui.comboBox)
        if tableNode and tableNode.IsA("vtkMRMLTableNode"):
            return tableNode

        inputName = inputVolumeNode.GetName() if inputVolumeNode else "Scan"
        segmentationName = segmentationNode.GetName() if segmentationNode else "Seg"
        tableName = f"Radiomics_{segmentationName}"
        tableNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", tableName)
        self._refreshComboBoxes()
        for index in range(self._comboCount(self.ui.comboBox)):
            if self.ui.comboBox.itemData(index) == tableNode.GetID():
                self.ui.comboBox.setCurrentIndex(index)
                break
        return tableNode

    def _checkCanApply(self, caller=None, event=None) -> None:
        inputReady = bool(self.ui.inputSelector.currentNode())
        segmentationReady = bool(self._selectedComboNode(self.ui.comboBox_inputSegmentation))
        hasMetric = any(self._isChecked(checkBox) for checkBox in self._metricCheckBoxes)
        canApply = inputReady and segmentationReady and hasMetric

        if canApply:
            self.ui.applyButton.toolTip = _("Create the table")
            self.ui.applyButton.enabled = True
        else:
            self.ui.applyButton.toolTip = _("Select a PET volume, a lesion segmentation mask and at least one radiomic to compute.")
            self.ui.applyButton.enabled = False

    def _showHelpMessage(self,title: str, message: str) -> None:
        try:
            qt.QMessageBox.information(slicer.util.mainWindow(), title, message)
        except Exception:
            slicer.util.showStatusMessage(message,8000)

    def _onHelpClicked(self,checked:bool = False) -> None:
        self._showHelpMessage(
            _("Radiomics"),
            _(
                "Radiomics definition\n"
                "TSurface (mm2) : surface of the tumour \n"
                "TVol (mm3) : volume of the tumour \n"
                "TumBB : tumor bounding box volume (mm3) \n"
                "First Order : first order radiomics features (from pyradiomics)\n"
                "medPCD : median percentile center distance, median distance between voxels and center of mass \n"
                "90PCD : 90th percentile center distance, distance at 90e percentile between voxels and center of mass \n"
                "medEdgePCD : median thickness measured across 3D directions \n"
                "nROI : number of regions of interest (lesions detected) \n"
                "Dmax : maximum distance between tumor centroids (mm) \n"
                "SUVmean : mean standardized uptake value across tumor region \n"
                "SUVmax : maximum standardized uptake value in tumor region \n"
                "TMTV : total metabolic tumor volume (cm3), sum of all lesions volumes \n"
                "TVSR : tumor volume to surface ratio (mm) = TMTV / TMTS \n"
                "TLG : total lesion glycolysis (SUV·cm3) = TMTV * SUVmean \n"
                "TMTS : total metabolic tumor surface (cm2), sum of all lesions surfaces \n"
                "NHOC : normalized distance between hotspot (SUVmax) and tumor centroid \n"
                "NHOP : normalized closest distance between hotspot (SUVmax) and tumor perimeter \n"
                "sdSphericity : standard deviation of sphericity across all lesions \n"
            ),
        )

    def onApplyButton(self) -> None:
        """Run processing when user clicks "Apply" button."""
        with slicer.util.tryWithErrorDisplay(_("Failed to compute radiomics."), waitCursor=True):
            progressDialog = slicer.util.createProgressDialog(
                value=0,
                maximum=0,  # indeterminate / busy indicator
                labelText=_("Initialisation..."),
                windowTitle=_("Extraction radiomique"),
                showCancelButton=True,
            )
            progressDialog.setMinimumDuration(0)
            completed = False
            outputTableNode = None
            try:
                try:
                    progressDialog.setWindowModality(qt.Qt.WindowModal)
                except Exception:
                    pass

                def reportProgress(message: str) -> None:
                    if progressDialog.wasCanceled:
                        raise RuntimeError(_("Extraction annulée."))
                    progressDialog.setLabelText(str(message))
                    try:
                        slicer.app.processEvents()
                    except Exception:
                        qt.QCoreApplication.processEvents()

                reportProgress(_("Lecture des entrées..."))
                inputVolumeNode = self.ui.inputSelector.currentNode()
                segmentationNode = self._selectedComboNode(self.ui.comboBox_inputSegmentation)
                outputTableNode = self._resolveOutputTableNode(inputVolumeNode, segmentationNode)
                selectedMetrics = self._selectedMetrics()
                self.logic.process(
                    inputVolumeNode=inputVolumeNode,
                    segmentationNode=segmentationNode,
                    outputTableNode=outputTableNode,
                    selectedMetrics=selectedMetrics,
                    progressCallback=reportProgress,
                )
                self.ui.applyButton.toolTip = _("Radiomics table created")
                completed = True

                reportProgress(_("Terminé."))
            finally:
                try:
                    progressDialog.close()
                except Exception:
                    pass

            if completed and outputTableNode is not None:
                title = _("Calcul terminé")
                message = _("Extraction radiomique terminée.\nTable : {tableName}").format(
                    tableName=outputTableNode.GetName() if outputTableNode else ""
                )
                try:
                    qt.QMessageBox.information(slicer.util.mainWindow(), title, message)
                except Exception:
                    slicer.util.showStatusMessage(message, 8000)


#
# RadiomicsModuleLogic
#


class RadiomicsModuleLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)

    @staticmethod
    def _coerceNumericScalar(value):
        """Convert common numeric scalar containers (numpy 0-d arrays, numpy scalars) to Python float.

        Returns None when value is not a scalar numeric.
        """
        if isinstance(value, (float, int, np.integer, np.floating)):
            return float(value)

        # numpy scalar (np.generic), e.g. np.float64(1.2)
        if isinstance(value, np.generic):
            try:
                return float(value)
            except Exception:
                return None

        # 0-d or 1-element numpy array, e.g. array(1.2) or array([1.2])
        if isinstance(value, np.ndarray):
            try:
                if value.shape == ():
                    return float(value.item())
                if value.size == 1:
                    return float(np.asarray(value).reshape(-1)[0])
            except Exception:
                return None
        return None

    @staticmethod
    def _imagesHaveSameGeometry(a: sitk.Image, b: sitk.Image) -> bool:
        if a.GetDimension() != b.GetDimension():
            return False
        if a.GetSize() != b.GetSize():
            return False
        # Use exact comparisons first; fall back to tolerances to avoid tiny float discrepancies.
        if a.GetOrigin() != b.GetOrigin():
            if not np.allclose(np.array(a.GetOrigin(), dtype=float), np.array(b.GetOrigin(), dtype=float), atol=1e-6):
                return False
        if a.GetSpacing() != b.GetSpacing():
            if not np.allclose(np.array(a.GetSpacing(), dtype=float), np.array(b.GetSpacing(), dtype=float), atol=1e-6):
                return False
        if a.GetDirection() != b.GetDirection():
            if not np.allclose(np.array(a.GetDirection(), dtype=float), np.array(b.GetDirection(), dtype=float), atol=1e-6):
                return False
        return True

    @staticmethod
    def _resampleToReference(maskImage: sitk.Image, referenceImage: sitk.Image, interpolator=sitk.sitkNearestNeighbor, defaultValue=0) -> sitk.Image:
        # Resample mask into reference image space (size/spacing/origin/direction).
        return sitk.Resample(maskImage, referenceImage, sitk.Transform(), interpolator, defaultValue, maskImage.GetPixelID())

    @staticmethod
    def _toSimpleItkFromVolumeNode(volumeNode: vtkMRMLScalarVolumeNode):
        return sitk.Cast(sitkUtils.PullVolumeFromSlicer(volumeNode), sitk.sitkFloat32)

    def _toSimpleItkFromSegmentationNode(self, segmentationNode, referenceVolumeNode):
        if segmentationNode.IsA("vtkMRMLLabelMapVolumeNode"):
            return sitk.Cast(sitkUtils.PullVolumeFromSlicer(segmentationNode), sitk.sitkUInt16)

        if not segmentationNode.IsA("vtkMRMLSegmentationNode"):
            raise ValueError("Lesions segmentation must be a segmentation node")

        temporaryLabelMap = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "__radiomics_tmp_labelmap__")
        try:
            success = slicer.modules.segmentations.logic().ExportVisibleSegmentsToLabelmapNode(
                segmentationNode, temporaryLabelMap, referenceVolumeNode
            )
            if not success:
                raise RuntimeError("Failed to export segmentation to labelmap")
            return sitk.Cast(sitkUtils.PullVolumeFromSlicer(temporaryLabelMap), sitk.sitkUInt16)
        finally:
            slicer.mrmlScene.RemoveNode(temporaryLabelMap)

    def _pcd_for_mask(self, mask_arr, spacing_zyx, percentile=None):
        indices_zyx = np.argwhere(mask_arr)
        if indices_zyx.size == 0:
            return 0.0

        center_of_mass_zyx = np.mean(indices_zyx, axis=0)
        distances_mm = np.linalg.norm((indices_zyx - center_of_mass_zyx) * spacing_zyx, axis=1)
        if percentile is None:
            return float(np.median(distances_mm))
        return float(np.percentile(distances_mm, percentile))

    def _calculate_medPCD(self, composantes_connexes, lesion_id=None):        # Implementation inspired from temp/radiomics/dispersion_feature_extractor.py
        # Compute median distance from lesion centroid to contour (boundary) points in mm.
        labels_arr = sitk.GetArrayFromImage(composantes_connexes)
        spacing_zyx = np.array(composantes_connexes.GetSpacing(), dtype=float)[::-1]

        def _medpcd_for_mask(mask_arr, lesion_id_local=None):
            if mask_arr.size == 0 or not np.any(mask_arr):
                return 0.0

            # centroid in index (zyx) space
            indices = np.argwhere(mask_arr)
            center_idx_zyx = np.mean(indices, axis=0)
            center_mm = center_idx_zyx[::-1].astype(float) * spacing_zyx

            # boundary voxels = mask - eroded(mask)
            mask_sitk = sitk.Cast(composantes_connexes == (1 if lesion_id_local is None else lesion_id_local), sitk.sitkUInt8)
            eroded = sitk.BinaryErode(mask_sitk, [1, 1, 1])
            boundary = sitk.Subtract(mask_sitk, eroded)
            boundary_arr = sitk.GetArrayFromImage(boundary).astype(bool)
            boundary_indices = np.argwhere(boundary_arr)
            if boundary_indices.size == 0:
                # fallback: use all voxels
                points_mm = indices[:, ::-1].astype(float) * spacing_zyx
            else:
                points_mm = boundary_indices[:, ::-1].astype(float) * spacing_zyx

            distances = np.linalg.norm(points_mm - center_mm, axis=1)
            return float(np.median(distances)) if distances.size else 0.0

        if lesion_id is not None:
            return _medpcd_for_mask(labels_arr == lesion_id, lesion_id)

        lesion_ids = np.unique(labels_arr)
        lesion_ids = lesion_ids[lesion_ids > 0]
        if lesion_ids.size == 0:
            return 0.0

        per_lesion = [
            _medpcd_for_mask(labels_arr == int(lid), int(lid)) for lid in lesion_ids
        ]
        return float(np.median(per_lesion))

    def _calculate_90thPCD(self, composantes_connexes, lesion_id):
        # distances en mm (prise en compte du spacing)
        labels_arr = sitk.GetArrayFromImage(composantes_connexes)
        spacing_zyx = np.array(composantes_connexes.GetSpacing(), dtype=float)[::-1]

        if lesion_id is not None:
            return self._pcd_for_mask(labels_arr == lesion_id, spacing_zyx, percentile=90)

        lesion_ids = np.unique(labels_arr)
        lesion_ids = lesion_ids[lesion_ids > 0]
        if lesion_ids.size == 0:
            return 0.0

        per_lesion = [self._pcd_for_mask(labels_arr == lid, spacing_zyx, percentile=90) for lid in lesion_ids]
        return float(np.median(per_lesion))

    def _unit_directions_3d_13(self):
        # création des directions unitaires pour les 13 directions canoniques en 3D (axes, diagonales de faces, diagonales d'espace)
        directions = []
        seen = set()
        for dx, dy, dz in itertools.product((-1, 0, 1), repeat=3):
            if dx == 0 and dy == 0 and dz == 0:
                continue

            v = np.array([dx, dy, dz], dtype=float)

            # pour éviter les directions opposées (ex: (1,0,0) et (-1,0,0)) on peut forcer la première composante non nulle à être positive
            nonzero = np.flatnonzero(v)
            if nonzero.size > 0 and v[nonzero[0]] < 0:
                v = -v

            # on normalise pour avoir des directions unitaires
            # vecteur unitaire pour les diagonales aussi
            # ie si on a (1 1 1) on veut (1/sqrt(3), 1/sqrt(3), 1/sqrt(3))
            v = v / np.linalg.norm(v)
            
            # on arrondit pour éviter les problèmes de précision qui feraient que des directions identiques ne soient pas reconnues comme telles
            # 8 décimales
            key = tuple(np.round(v, 8))
            if key in seen:
                continue
            seen.add(key)
            directions.append(v)

        # 13 directions canoniques : 6 axes (1,0,0), (0,1,0), (0,0,1) et leurs opposés ; 6 diagonales de faces (1,1,0), (1,0,1), (0,1,1) et leurs opposés ; 1 diagonale d'espace (1,1,1) et son opposé
        return np.asarray(directions, dtype=float)

    def _thickness_mm_from_points(self, points_mm_xyz, spacing_mm_xyz, directions):
        # calculer l'epaisseur d'une tumeur à partir de ses points en mm, du spacing et des directions à tester.
        if points_mm_xyz.size == 0: # si pas de points, épaisseur 0
            return 0.0

        thicknesses = [] # l'epaisseur pour chaque direction est d'abord à 0
        for u in directions: # on teste ensuite toutes les directions
            proj = points_mm_xyz @ u  # shape (N,) ; on fait le projeté des points dans la direction
            base = float(proj.max() - proj.min())  # épaisseur de base : distance entre les points les plus éloignés projetés sur la direction
            voxel_extent = float(np.dot(np.abs(u), spacing_mm_xyz)) #
            thicknesses.append(base + voxel_extent)

        return float(np.median(thicknesses))

    def _thickness_for_mask(self, mask_arr, spacing_mm_xyz, directions):
        indices_zyx = np.argwhere(mask_arr)
        if indices_zyx.size == 0:
            return 0.0
        points_mm_xyz = indices_zyx[:, ::-1].astype(float) * spacing_mm_xyz
        return self._thickness_mm_from_points(points_mm_xyz, spacing_mm_xyz, directions)

    def _calculate_medEdgePCD(self, composantes_connexes, lesion_id):
        #interpretation used here:
        # - for one lesion: median thickness across 13 canonical 3D directions.
        # - for multiple lesions (lesion_id=None): compute per lesion and return the
        #  median across lesions (so one huge lesion doesn't dominate).

        labels_arr = sitk.GetArrayFromImage(composantes_connexes)
        spacing_mm_xyz = np.array(composantes_connexes.GetSpacing(), dtype=float)
        directions = self._unit_directions_3d_13()

        if lesion_id is not None:
            return self._thickness_for_mask(labels_arr == lesion_id, spacing_mm_xyz, directions)

        lesion_ids = np.unique(labels_arr)
        lesion_ids = lesion_ids[lesion_ids > 0]
        if lesion_ids.size == 0:
            return 0.0

        per_lesion = [self._thickness_for_mask(labels_arr == lid, spacing_mm_xyz, directions) for lid in lesion_ids]
        return float(np.median(per_lesion))

    def calculate_TVSR(self, TMTV, TMTS):
        if TMTS <= 0.0:
            return float('nan') # division par 0
        
        # mm^3 / mm^2 = mm
        return TMTV / TMTS

    def _calculate_TVSR_local(self, composantes_connexes, lesion_id):
        geometrie = sitk.LabelShapeStatisticsImageFilter()
        geometrie.ComputePerimeterOn()
        geometrie.Execute(composantes_connexes)
        volume_mm3 = float(geometrie.GetPhysicalSize(lesion_id))
        surface_mm2 = float(geometrie.GetPerimeter(lesion_id))
        if surface_mm2 <= 0.0:
            return float("nan")
        return volume_mm3 / surface_mm2

    def _calculate_TUMBB(self, composantes_connexes, lesion_id):
        geometrie = sitk.LabelShapeStatisticsImageFilter()
        geometrie.Execute(composantes_connexes)
        bb = geometrie.GetBoundingBox(lesion_id)
        # si un seul voxel, on evite erreur de bounding box
        if any(s==0 for s in bb[3:]):
            return 0.0
        size_index = np.array(bb[3:], dtype=float)
        spacing_mm = np.array(composantes_connexes.GetSpacing(), dtype=float)
        size_mm = size_index * spacing_mm
        volume_mm3 = float(np.prod(size_mm))
        return volume_mm3 / 1000.0

    def _calculate_SUV_max(self, scan, composantes_connexes):
        scan_array = sitk.GetArrayFromImage(scan)
        # temp extractors expect lesion label == 1
        mask_array = sitk.GetArrayFromImage(composantes_connexes) == 1
        vals = scan_array[mask_array]
        if vals.size == 0:
            return 0.0
        return float(np.max(vals))

    def _calculate_SUV_mean(self, scan, composantes_connexes):
        scan_array = sitk.GetArrayFromImage(scan)
        mask_array = sitk.GetArrayFromImage(composantes_connexes) == 1
        vals = scan_array[mask_array]
        if vals.size == 0:
            return 0.0
        return float(np.mean(vals))

    def _calculate_TMTV(self, composantes_connexes):
        geometrie = sitk.LabelShapeStatisticsImageFilter()
        geometrie.Execute(composantes_connexes)
        total_mm3 = 0.0
        for lesion_id in geometrie.GetLabels():
            total_mm3 += float(geometrie.GetPhysicalSize(lesion_id))
        return total_mm3 / 1000.0

    def _calculate_TLG(self, scan, composantes_connexes):
        suv_mean = self._calculate_SUV_mean(scan, composantes_connexes)
        voxel_volume_cm3 = np.prod(composantes_connexes.GetSpacing(), dtype=float) / 1000.0
        count = np.bincount(sitk.GetArrayFromImage(composantes_connexes).astype(int).flatten())
        volume = count * voxel_volume_cm3
        if volume.size <= 1:
            return 0.0
        return float(suv_mean * volume[1])

    def _calculate_TMTS(self, composantes_connexes):
        # Use marching cubes on the binary mask to estimate surface area
        _ensurePythonPackage("scikit-image", "skimage")
        try:
            from skimage import measure
        except ImportError as error:
            raise RuntimeError(
                "The TMTS metric requires scikit-image in Slicer's Python environment."
            ) from error

        mask = (sitk.GetArrayFromImage(composantes_connexes) > 0).astype(np.uint8)
        if mask.max() == 0:
            return 0.0
        verts, faces, normals, values = measure.marching_cubes(mask, level=0.5, spacing=composantes_connexes.GetSpacing()[::-1])
        tmts = measure.mesh_surface_area(verts, faces)
        return float(tmts)

    def _calculate_Dmax(self, composantes_connexes):
        _ensurePythonPackage("scipy", "scipy")
        try:
            from scipy.spatial import ConvexHull
            from scipy.spatial.distance import pdist
        except ImportError as error:
            raise RuntimeError(
                "The Dmax metric requires SciPy in Slicer's Python environment."
            ) from error

        arr = sitk.GetArrayFromImage(composantes_connexes) > 0
        coords = np.argwhere(arr)
        if coords.size == 0:
            return 0.0
        # Convex hull in index space, then transform hull vertices to physical points
        try:
            hull = ConvexHull(coords)
            hull_points = coords[hull.vertices]
        except Exception:
            # Fallback: use all coords
            hull_points = coords

        origin = np.array(composantes_connexes.GetOrigin(), dtype=float)
        spacing = np.array(composantes_connexes.GetSpacing(), dtype=float)
        direction = np.array(composantes_connexes.GetDirection(), dtype=float).reshape(3, 3)

        def _idx_zyx_to_xyz_mm(idx_zyx):
            return origin + direction @ (spacing * np.array(idx_zyx[::-1], dtype=float))

        physical = [
            _idx_zyx_to_xyz_mm(tuple(int(x) for x in h)) for h in hull_points
        ]
        if len(physical) < 2:
            return 0.0
        distances = pdist(physical, metric='euclidean')
        return float(np.max(distances))

    def _calculate_nROI(self, segmentation):
        label_filter = sitk.ConnectedComponentImageFilter()
        label_filter.Execute(segmentation > 0)
        return int(label_filter.GetObjectCount())

    def _calculate_NHOC(self, scan, composantes_connexes, lesion_id=None):
        '''
        Distance entre d(H,C), avec H le hotspot (voxel max) et C le centroid,
        normalisée par le rayon d'une sphère hypothétique ayant le même volume que la tumeur.
        '''
        scan_arr = sitk.GetArrayFromImage(scan)
        labels_arr = sitk.GetArrayFromImage(composantes_connexes)

        geom = sitk.LabelShapeStatisticsImageFilter()
        geom.Execute(composantes_connexes)

        origin = np.array(composantes_connexes.GetOrigin(), dtype=float)
        spacing = np.array(composantes_connexes.GetSpacing(), dtype=float)
        direction = np.array(composantes_connexes.GetDirection(), dtype=float).reshape(3, 3)

        def _zyx_to_xyz_mm(index_zyx):
            return origin + direction @ (spacing * np.array(index_zyx[::-1], dtype=float))

        def nhoc_for_lesion(lid):
            mask = labels_arr == lid
            hotspot_zyx = np.unravel_index(int(np.argmax(np.where(mask, scan_arr, -np.inf))), scan_arr.shape)
            d_HC = float(np.linalg.norm(_zyx_to_xyz_mm(hotspot_zyx) - np.array(geom.GetCentroid(lid), dtype=float)))
            V = float(geom.GetPhysicalSize(lid))
            r = (3.0 * V / (4.0 * np.pi)) ** (1.0 / 3.0)
            if r <= 0.0:
                return float("nan")
            return d_HC / r

        if lesion_id is not None:
            return nhoc_for_lesion(lesion_id)

        lesion_ids = list(geom.GetLabels())
        if not lesion_ids:
            return 0.0
        return float(np.median([nhoc_for_lesion(lid) for lid in lesion_ids]))

    def _calculate_NHOP(self, scan, composantes_connexes, lesion_id=None):
        '''
        Distance minimale entre d(H,P), avec H le hotspot (voxel max) et P le périmètre,
        normalisée par le rayon d'une sphère hypothétique ayant le même volume que la tumeur.
        '''
        scan_arr = sitk.GetArrayFromImage(scan)
        labels_arr = sitk.GetArrayFromImage(composantes_connexes)
        spacing_zyx = np.array(composantes_connexes.GetSpacing(), dtype=float)[::-1]

        geom = sitk.LabelShapeStatisticsImageFilter()
        geom.Execute(composantes_connexes)

        def nhop_for_lesion(lid):
            mask = labels_arr == lid
            hotspot_zyx = np.unravel_index(
                int(np.argmax(np.where(mask, scan_arr, -np.inf))), scan_arr.shape
            )

            # Voxels de bord = masque - masque érodé
            mask_sitk = sitk.Cast(composantes_connexes == lid, sitk.sitkUInt8)
            eroded = sitk.BinaryErode(mask_sitk, [1, 1, 1])
            boundary_zyx = np.argwhere(
                sitk.GetArrayFromImage(sitk.Subtract(mask_sitk, eroded)).astype(bool)
            )
            if boundary_zyx.size == 0:
                return float("nan")

            hotspot_mm = np.array(hotspot_zyx, dtype=float) * spacing_zyx
            boundary_mm = boundary_zyx.astype(float) * spacing_zyx
            d_HP = float(np.min(np.linalg.norm(boundary_mm - hotspot_mm, axis=1)))

            V = float(geom.GetPhysicalSize(lid))
            r = (3.0 * V / (4.0 * np.pi)) ** (1.0 / 3.0)
            if r <= 0.0:
                return float("nan")
            return d_HP / r

        if lesion_id is not None:
            return nhop_for_lesion(lesion_id)

        lesion_ids = list(geom.GetLabels())
        if not lesion_ids:
            return 0.0
        return float(np.median([nhop_for_lesion(lid) for lid in lesion_ids]))
    
    def _calculate_sphericity(self, composantes_connexes, lesion_id):
        geom = sitk.LabelShapeStatisticsImageFilter()
        geom.ComputePerimeterOn()
        geom.Execute(composantes_connexes)

        V = float(geom.GetPhysicalSize(lesion_id))
        S = float(geom.GetPerimeter(lesion_id))

        if S <= 0:
            return float("nan")

        return (
            np.pi ** (1.0 / 3.0)
            * (6.0 * V) ** (2.0 / 3.0)
            / S
        )

    def _calculate_meanSphericity(self, composantes_connexes):
        geom = sitk.LabelShapeStatisticsImageFilter()
        geom.ComputePerimeterOn()
        geom.Execute(composantes_connexes)

        sph = []

        for lid in geom.GetLabels():
            V = float(geom.GetPhysicalSize(lid))
            S = float(geom.GetPerimeter(lid))

            if S > 0:
                sph.append(
                    np.pi ** (1/3)
                    * (6*V) ** (2/3)
                    / S
                )

        if len(sph) < 2:
            return 0.0

        return float(np.mean(sph))

    def _calculate_sdSphericity(self, composantes_connexes):
        geom = sitk.LabelShapeStatisticsImageFilter()
        geom.ComputePerimeterOn()
        geom.Execute(composantes_connexes)

        sph = []

        for lid in geom.GetLabels():
            V = float(geom.GetPhysicalSize(lid))
            S = float(geom.GetPerimeter(lid))

            if S > 0:
                sph.append(
                    np.pi ** (1/3)
                    * (6*V) ** (2/3)
                    / S
                )

        if len(sph) < 2:
            return 0.0

        return float(np.std(sph))


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

        vtkColumns = []
        for columnName in columns:
            column = vtk.vtkStringArray()
            column.SetName(columnName)
            table.AddColumn(column)
            vtkColumns.append(column)

        table.SetNumberOfRows(len(rows))
        for rowIndex, row in enumerate(rows):
            for columnIndex, columnName in enumerate(columns):
                value = row.get(columnName, "")
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    value = ""
                table.SetValue(rowIndex, columnIndex, str(value))

        outputTableNode.Modified()

    def process(self,
                inputVolumeNode: vtkMRMLScalarVolumeNode,
                segmentationNode,
                outputTableNode: vtkMRMLTableNode,
                selectedMetrics: dict,
                progressCallback: Callable[[str], None] | None = None) -> None:
        if not inputVolumeNode:
            raise ValueError("Invalid volume")
        if not segmentationNode:
            raise ValueError("Invalid segmentation")
        if not outputTableNode:
            raise ValueError("Invalide output table")

        def progress(message: str) -> None:
            if progressCallback is not None:
                progressCallback(message)

        progress(_("Conversion du volume PET (SimpleITK)..."))

        scanImage = self._toSimpleItkFromVolumeNode(inputVolumeNode)

        progress(_("Conversion de la segmentation (SimpleITK)..."))
        segmentationImage = self._toSimpleItkFromSegmentationNode(segmentationNode, inputVolumeNode)

        anySelected = any(bool(v) for v in (selectedMetrics or {}).values())

        progress(_("Vérification / alignement de la géométrie..."))
        # Make sure segmentation is in the same geometry as the scan.
        # This is especially important when the input segmentation is a labelmap volume node.
        if not self._imagesHaveSameGeometry(segmentationImage, scanImage):
            segmentationImage = self._resampleToReference(segmentationImage, scanImage, interpolator=sitk.sitkNearestNeighbor, defaultValue=0)

        progress(_("Détection des lésions (composantes connexes)..."))
        binaryMask = segmentationImage > 0
        composantes_connexes = sitk.ConnectedComponent(binaryMask)

        progress(_("Calcul des statistiques de forme..."))
        geometrie = sitk.LabelShapeStatisticsImageFilter()
        geometrie.ComputePerimeterOn()
        geometrie.Execute(composantes_connexes)

        progress(_("Calculating metrics..."))
        # UI is now global-only, so selectedMetrics contains all metrics directly
        globalRadiomics = {}
        if selectedMetrics.get("nROI"):
            progress(_("Calculating nROI..."))
            globalRadiomics["nROI"] = self._calculate_nROI(segmentationImage)
        if selectedMetrics.get("Dmax"):
            progress(_("Calculating Dmax..."))
            globalRadiomics["Dmax"] = self._calculate_Dmax(composantes_connexes)
        if selectedMetrics.get("SUVmean"):
            progress(_("Calculating SUVmean..."))
            globalRadiomics["SUVmean"] = self._calculate_SUV_mean(scanImage, composantes_connexes)
        if selectedMetrics.get("SUVmax"):
            progress(_("Calculating SUVmax..."))
            globalRadiomics["SUVmax"] = self._calculate_SUV_max(scanImage, composantes_connexes)
        if selectedMetrics.get("TMTV"):
            progress(_("Calculating TMTV..."))
            globalRadiomics["TMTV"] = self._calculate_TMTV(composantes_connexes)
        if selectedMetrics.get("TLG"):
            progress(_("Calculating TLG..."))
            globalRadiomics["TLG"] = self._calculate_TLG(scanImage, composantes_connexes)
        if selectedMetrics.get("TMTS"):
            progress(_("Calculating TMTS..."))
            globalRadiomics["TMTS"] = self._calculate_TMTS(composantes_connexes)
        if selectedMetrics.get("surface_tumorale_mm2"):
            progress(_("Calculating surface tumorale (mm²)..."))
            total_surface_mm2 = 0.0
            for lesion_id in geometrie.GetLabels():
                total_surface_mm2 += geometrie.GetPerimeter(lesion_id)
            globalRadiomics["surface_tumorale_mm2"] = total_surface_mm2
        if selectedMetrics.get("volume_tumoral_mm3"):
            progress(_("Calculating volume tumoral (mm³)..."))
            total_volume_mm3 = 0.0
            for lesion_id in geometrie.GetLabels():
                total_volume_mm3 += geometrie.GetPhysicalSize(lesion_id)
            globalRadiomics["volume_tumoral_mm3"] = total_volume_mm3
        if selectedMetrics.get("medPCD"):
            progress(_("Calculating medPCD..."))
            globalRadiomics["medPCD"] = self._calculate_medPCD(composantes_connexes)
        if selectedMetrics.get("90thPCD"):
            progress(_("Calculating 90thPCD..."))
            globalRadiomics["90thPCD"] = self._calculate_90thPCD(composantes_connexes, None)
        if selectedMetrics.get("medEdgePCD"):
            progress(_("Calculating medEdgePCD..."))
            globalRadiomics["medEdgePCD"] = self._calculate_medEdgePCD(composantes_connexes, None)
        if selectedMetrics.get("TVSR"):
            progress(_("Calculating TVSR..."))
            # TVSR should be computable standalone: compute dependencies internally
            # without necessarily displaying them in the output table.
            tmtv_value = globalRadiomics.get("TMTV")
            tmts_value = globalRadiomics.get("TMTS")
            if tmtv_value is None:
                tmtv_value = self._calculate_TMTV(composantes_connexes)
            if tmts_value is None:
                tmts_value = self._calculate_TMTS(composantes_connexes)

            if tmts_value <= 0.0:
                raise RuntimeError("TMTS is 0; cannot compute TVSR (division by zero).")

            globalRadiomics["TVSR"] = self.calculate_TVSR(tmtv_value, tmts_value)
        if selectedMetrics.get("TumBB"):
            progress(_("Calculating TumBB..."))
            total_tumbb = 0.0
            for lesion_id in geometrie.GetLabels():
                total_tumbb += self._calculate_TUMBB(composantes_connexes, lesion_id)
            globalRadiomics["TumBB"] = total_tumbb
        if selectedMetrics.get("NHOC"):
            progress(_("Calculating NHOC..."))
            globalRadiomics["NHOC"] = self._calculate_NHOC(scanImage, composantes_connexes, None)
        if selectedMetrics.get("NHOP"):
            progress(_("Calculating NHOP..."))
            globalRadiomics["NHOP"] = self._calculate_NHOP(scanImage, composantes_connexes, None)
        if selectedMetrics.get("sdSphericity"):
            progress(_("Calculating sdSphericity..."))
            globalRadiomics["sdSphericity"] = self._calculate_sdSphericity(composantes_connexes)
            globalRadiomics["meanSphericity"] = self._calculate_meanSphericity(composantes_connexes)
        firstOrderExtractor = None
        if selectedMetrics.get("FirstOrder"):
            progress(_("Initialisation PyRadiomics (FirstOrder)..."))
            try:
                from radiomics import featureextractor as pyradiomicsFeatureExtractor
                firstOrderExtractor = pyradiomicsFeatureExtractor.RadiomicsFeatureExtractor(binWidth=0.25)
                firstOrderExtractor.disableAllFeatures()
                firstOrderExtractor.enableFeatureClassByName("firstorder")
                # Be explicit about image type; in some configurations execute() may otherwise return only diagnostics.
                try:
                    firstOrderExtractor.enableImageTypeByName("Original")
                except Exception:
                    pass
            except Exception as error:
                raise RuntimeError(
                    "PyRadiomics is unavailable. Install the SlicerRadiomics extension "
                    "from the 3D Slicer Extension Manager, then restart Slicer."
                ) from error
 
        rows = []
        if globalRadiomics:
            globalRow = {"scope": "global"}
            globalRow.update(globalRadiomics)
            rows.append(globalRow)

        if firstOrderExtractor is not None:
            progress(_("Calculating PyRadiomics FirstOrder..."))
            # Ensure we have a target row even if user selected only FirstOrder.
            if not rows:
                rows = [{"scope": "global"}]

            # First-order features should be computed on the union of all lesions.
            # PyRadiomics expects a label/binary mask; use a binary mask to include all components.
            firstOrderMask = sitk.Cast(binaryMask, sitk.sitkUInt8)
            maskSum = int(sitk.GetArrayViewFromImage(firstOrderMask).sum())
            if maskSum == 0:
                raise RuntimeError("Segmentation mask is empty (no voxel > 0). Cannot compute first-order radiomics.")

            try:
                # Force label=1 for a binary (0/1) mask. Some PyRadiomics versions require it explicitly.
                try:
                    globalFeatures = firstOrderExtractor.execute(scanImage, firstOrderMask, label=1)
                except TypeError:
                    globalFeatures = firstOrderExtractor.execute(scanImage, firstOrderMask)
            except Exception as error:
                # Do not swallow: let Slicer show the error (tryWithErrorDisplay in the widget).
                raise RuntimeError(f"First-order radiomics failed: {error}") from error

            progress(_("Normalisation des features PyRadiomics..."))

            added = 0
            for featureName, featureValue in globalFeatures.items():
                if "diagnostics" in featureName:
                    continue
                numericValue = self._coerceNumericScalar(featureValue)
                if numericValue is not None:
                    rows[0][f"FirstOrder_{featureName}"] = numericValue
                    added += 1
            if added == 0:
                diagnostics_keys = sorted([k for k in globalFeatures.keys() if "diagnostics" in k])
                non_diag_keys = sorted([k for k in globalFeatures.keys() if "diagnostics" not in k])
                unique_mask_values = np.unique(sitk.GetArrayViewFromImage(firstOrderMask))
                raise RuntimeError(
                    "First-order radiomics returned no numeric scalar features. "
                    f"maskUnique={unique_mask_values.tolist()}, maskSum={maskSum}, "
                    f"nonDiagnosticsKeys={non_diag_keys}, diagnosticsKeys={diagnostics_keys}"
                )

        if not rows:
            rows = [{"scope": "global"}]

        # If user selected at least one metric but we ended up with an empty result row,
        # surface it instead of silently writing only the scope.
        if anySelected and len(rows) == 1 and set(rows[0].keys()) == {"scope"}:
            raise RuntimeError(
                "No radiomics were computed. "
                "If you selected FirstOrder, check Slicer logs for the PyRadiomics error details."
            )

        progress(_("Écriture des résultats dans la table..."))
        self._rowsToTable(outputTableNode, rows)
        logging.info("Radiomics extraction finished: %d lines in the table %s", len(rows), outputTableNode.GetName())


#
# RadiomicsModuleTest
#


class RadiomicsModuleTest(ScriptedLoadableModuleTest):
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
        self.test_RadiomicsModule1()

    def test_RadiomicsModule1(self):
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

        self.delayDisplay("Starting RadiomicsModule smoke test")
        logic = RadiomicsModuleLogic()
        self.assertIsNotNone(logic)

        phantomArray = np.zeros((16, 16, 16), dtype=np.float32)
        phantomArray[4:12, 4:12, 4:12] = 7.0
        segmentationArray = (phantomArray > 0).astype(np.uint8)

        inputVolumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "RadiomicsPhantomPET")
        slicer.util.updateVolumeFromArray(inputVolumeNode, phantomArray)

        labelMapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "RadiomicsPhantomLabelMap")
        slicer.util.updateVolumeFromArray(labelMapNode, segmentationArray)

        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "RadiomicsPhantomSegmentation")
        slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelMapNode, segmentationNode)

        outputTableNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", "RadiomicsPhantomResults")
        logic.process(
            inputVolumeNode=inputVolumeNode,
            segmentationNode=segmentationNode,
            outputTableNode=outputTableNode,
            selectedMetrics={"nROI": True, "SUVmean": True, "SUVmax": True, "TMTV": True},
        )

        table = outputTableNode.GetTable()
        self.assertEqual(table.GetNumberOfRows(), 1)
        self.assertAlmostEqual(table.GetValueByName(0, "nROI").ToDouble(), 1.0)
        self.assertAlmostEqual(table.GetValueByName(0, "SUVmean").ToDouble(), 7.0)
        self.assertAlmostEqual(table.GetValueByName(0, "SUVmax").ToDouble(), 7.0)
        self.assertGreater(table.GetValueByName(0, "TMTV").ToDouble(), 0.0)
        self.delayDisplay("RadiomicsModule smoke test passed")
