-module(shapes).
-export([area/1, describe/1]).

area({circle, R}) ->
    math:pi() * square(R);
area({square, S}) ->
    square(S);
area(_) ->
    0.

square(X) ->
    X * X.

describe(Shape) ->
    io:format("~p~n", [area(Shape)]).
