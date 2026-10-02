function(shapes_square out x)
  math(EXPR result "${x} * ${x}")
  set(${out} ${result} PARENT_SCOPE)
endfunction()

macro(shapes_area out r)
  shapes_square(sq ${r})
  math(EXPR ${out} "3 * ${sq}")
endmacro()

shapes_area(area 2)
message(STATUS "area: ${area}")
