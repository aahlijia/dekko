#!/bin/zsh

square() {
  print $(( $1 * $1 ))
}

function area {
  local r="$1"
  print $(( 3 * $(square "$r") ))
}

area 2
