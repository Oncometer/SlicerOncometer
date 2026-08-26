# OncometerExtension

3D Slicer extension for oncology segmentation and quantification.

## Main Modules

* **SegmentLesionsModule**: segmentation of tumor lesions on PET/CT volumes.
* **SegmentOrgansModule**: organ segmentation from CT images.

As well as feature extraction from these segmentations:

* **RadiomicsModule**: extraction of radiomic measurements from a PET image and a segmentation mask.
* **OrganomicsModule**: organ-related quantification tools.

## Installation

1. Copy or clone the `OncometerExtension` folder.
2. Launch 3D Slicer.
3. Install the required dependencies below.
4. In the Extension Wizard in the 3d Slicer modules, choose the folder with "Select Extension"
5. Reload 3D Slicer.

### Python Dependencies

Some modules use Python packages. To use all modules, install LION and MOOSE in the 3D Slicer Python console by running the following commands:

```python
slicer.util.pip_install("--upgrade pip")

slicer.util.pip_install("lionz")

slicer.util.pip_install("moosez")
```

SlicerRadiomics must also be installed from the Extension Manager.

## nnU-Net Segmentation Models

The `SegmentLesionsModule` and `SegmentOrgansModule` include an ENHANCE PET model by default in the code:

* `ENHANCE PET Lion Segmentation` for lesions, with a binary output using label `1`.
* `ENHANCE-PET Moose Segmentation` for organs, with labels `0-9`. The labels are as follows:

  * 1: Spleen
  * 2: Kidneys
  * 3: Liver
  * 4: Pancreas
  * 5: Adrenal Glands
  * 6: Heart
  * 7: Brain
  * 8: Lungs
  * 9: Bones

Alternatively, nnUNet models can be added from the **"Custom nnUNet"** configuration page.

### Expected Model Weights Directory Structure

The results directory should contain the usual nnU-Net artifacts, with a structure such as:

```text
nnUNet_results/

    DatasetXXX_DatasetName/

        nnUNetTrainer__nnUNetPlans__3d_fullres/

            fold_0/

                checkpoint_final.pth

            fold_1/

                checkpoint_final.pth

            fold_2/

                checkpoint_final.pth

            fold_3/

                checkpoint_final.pth

            fold_4/

                checkpoint_final.pth
```

If your environment does not automatically expose `nnUNet_results`, specify the expected path in the module interface or in the configuration associated with the model.

### Best Practices

* Make sure that the model was trained for the same image modality as the input data.
* Test one new model before adding multiple models.
* Keep large model files outside the Git repository whenever possible.

## Adding a Radiomic Feature or Metric

The `RadiomicsModule` computes metrics from a PET image and a lesion segmentation.

The metrics are controlled by the UI in `RadiomicsModule/Resources/UI/RadiomicsModule.ui` and by the logic in `RadiomicsModule/RadiomicsModule.py`.

### Adding a Simple Metric

1. Add a new `QCheckBox` to `RadiomicsModule.ui`.
2. Retrieve it in the `_metricCheckBoxes` list in `RadiomicsModule.py`.
3. Add it to the `_selectedMetrics()` method using a stable key.
4. Add a calculation function for this metric in `RadiomicsModuleLogic`.
5. Implement the calculation in `RadiomicsModuleLogic.process()`.

### Adding a PyRadiomics Feature

1. Make sure PyRadiomics is installed in Slicer's Python environment.
2. Enable the desired feature class in `RadiomicsModuleLogic.process()`.
3. Convert the result to a scalar value before writing it to the output table.
4. Handle cases where the extractor does not return a numeric value.

The module already contains an example using the `FirstOrder` features.

### Points to Keep in Mind

* Non-numeric values must be filtered out before being written to the table.

## Expected Outputs

* The segmentation modules produce segmentation nodes or labelmaps.
* The Radiomics module writes its results to an MRML table.
* The quantification modules may create intermediate outputs in the Slicer scene.
