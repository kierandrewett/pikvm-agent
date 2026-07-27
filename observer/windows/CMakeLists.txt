cmake_minimum_required(VERSION 3.20)
project(pikvm_accuracy_observer LANGUAGES CXX)

add_executable(pikvm-accuracy-observer WIN32 PikvmObserver.cpp)
target_compile_features(pikvm-accuracy-observer PRIVATE cxx_std_20)
target_compile_definitions(
  pikvm-accuracy-observer
  PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
)
target_link_libraries(
  pikvm-accuracy-observer
  PRIVATE advapi32 bcrypt comctl32 crypt32 gdi32 shell32 winhttp
)

if(MINGW)
  target_compile_options(
    pikvm-accuracy-observer
    PRIVATE -Os -ffunction-sections -fdata-sections
  )
  target_link_options(
    pikvm-accuracy-observer
    PRIVATE
      -municode
      -static
      -static-libgcc
      -static-libstdc++
      -s
      -Wl,--gc-sections
  )
endif()
