module Circle = struct
  let square x = x *. x
  let area r = Float.pi *. square r
end

module type SHAPE = sig
  val area : float -> float
end

let describe r = string_of_float (Circle.area r)
let default_radius = 2.0
let () = print_endline (describe default_radius)
