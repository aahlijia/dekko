{ lib, pkgs, ... }:
let
  square = x: x * x;
  area = { radius, scale ? 1 }: 3 * scale * (square radius);
  defaults = { radius = 2; };
in
{
  options.shapes.area = lib.mkOption {
    type = lib.types.int;
    default = area defaults;
  };
  config.named = lib.nameValuePair "circle" (area { radius = 3; });
}
