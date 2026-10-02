module Shapes.Core

type Shape =
    | Circle of float
    | Square of float

type Printer() =
    member this.Print(shape: Shape) = describe shape
    static member Default = Printer()

let square x = x * x

let area shape =
    match shape with
    | Circle r -> System.Math.PI * square r
    | Square s -> square s

let describe shape = sprintf "%f" (area shape)

printfn "%s" (describe (Circle 2.0))
