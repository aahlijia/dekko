module Shapes exposing (Shape(..), area, describe)


type Shape
    = Circle Float
    | Square Float


type alias Model =
    { shape : Shape }


square : Float -> Float
square x =
    x * x


area : Shape -> Float
area shape =
    case shape of
        Circle r ->
            pi * square r

        Square s ->
            square s


describe : Model -> String
describe model =
    let
        value =
            area model.shape
    in
    String.fromFloat value
