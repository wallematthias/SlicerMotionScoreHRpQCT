cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(MotionScoreHRpQCT)

set(EXTENSION_HOMEPAGE "https://github.com/wallematthias/SlicerMotionScoreHRpQCT")
set(EXTENSION_CATEGORY "Quantification")
set(EXTENSION_CONTRIBUTORS "Matthias Walle (University of Calgary)")
set(EXTENSION_DESCRIPTION "Interactive HR-pQCT motion grading workflow for 3D Slicer that runs MotionScore predictions, supports rapid reviewer grading, and exports review-ready tables. No known related patents.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/wallematthias/SlicerMotionScoreHRpQCT/main/resources/icon.png")
set(EXTENSION_SCREENSHOTURLS "https://raw.githubusercontent.com/wallematthias/SlicerMotionScoreHRpQCT/main/resources/ScreenshotMotionScoreHRpQCT.png")
set(EXTENSION_DEPENDS "")

find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

add_subdirectory(MotionScoreHRpQCT)

include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
