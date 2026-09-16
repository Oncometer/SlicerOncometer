cmake_minimum_required(VERSION 3.20.6...3.22.6 FATAL_ERROR)

project(Oncometer)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/Oncometer/SlicerOncometer")
set(EXTENSION_CONTRIBUTORS "Charlotte Boucherie (Centre Henri Becquerel), Pr. Pierre Decazes (Centre Henri Becquerel)")
set(EXTENSION_DESCRIPTION "3D Slicer extension for lesions and organs segmentation with analysis")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/OncometerExtension.png")
set(EXTENSION_SCREENSHOTURLS "https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_SegmentLesions_1.png;https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_SegmentLesions_2.png;https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_SegmentOrgans_1.png;https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_SegmentOrgans_2.png;https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_Radiomics_1.png;https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_Radiomics_2.png;https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_Organomics_1.png;https://raw.githubusercontent.com/Oncometer/SlicerOncometer/main/Screenshots/Screenshot_Organomics_2.png")
set(EXTENSION_DEPENDS "SlicerRadiomics") # Specified as a list or "NA" if no dependencies
#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer 5.10.0 REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(SegmentLesionsModule)
add_subdirectory(RadiomicsModule)
add_subdirectory(SegmentOrgansModule)
add_subdirectory(OrganomicsModule)


#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
