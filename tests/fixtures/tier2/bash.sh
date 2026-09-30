#!/bin/bash

square() {
  echo $(( $1 * $1 ))
}

function area {
  local r="$1"
  echo $(( 3 * $(square "$r") ))
}

area 2
./other.sh --flag
