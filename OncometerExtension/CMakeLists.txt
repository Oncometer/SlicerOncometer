cmake_minimum_required(VERSION 3.20.6...3.22.6 FATAL_ERROR)

project(OncometerExtension)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://www.slicer.org/wiki/Documentation/Nightly/Extensions/OncometerExtension")
set(EXTENSION_CONTRIBUTORS "Charlotte Boucherie (Centre Henri Becquerel), Pr. Pierre Decazes (Centre Henri Becquerel)")
set(EXTENSION_DESCRIPTION "3D Slicer extension for lesions and organs segmentation with analysis")
set(EXTENSION_ICONURL "https://www.example.com/Slicer/Extensions/OncometerExtension.png")
set(EXTENSION_SCREENSHOTURLS "https://www.example.com/Slicer/Extensions/OncometerExtension/Screenshots/1.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
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
