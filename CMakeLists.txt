cmake_minimum_required(VERSION 3.16)
project(new C)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

file(GLOB SOURCES
    "${CMAKE_CURRENT_SOURCE_DIR}/dev/pltf/*.c"
    "${CMAKE_CURRENT_SOURCE_DIR}/dev/cfg/*.c"
)

add_library(new STATIC ${SOURCES})

target_include_directories(new PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/dev/pltf
    ${CMAKE_CURRENT_SOURCE_DIR}/dev/cfg
)

target_compile_options(new PRIVATE
    -Wall
    -Wextra
    -Wpedantic
)