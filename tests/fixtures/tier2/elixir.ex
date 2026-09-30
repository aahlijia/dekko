defmodule Shapes.Circle do
  defstruct radius: 0

  def area(%__MODULE__{radius: r}) when r > 0, do: :math.pi() * square(r)
  def area(_), do: 0

  defp square(x) do
    x * x
  end

  defmacro twice(x), do: quote(do: unquote(x) * 2)
end

defprotocol Shapes.Shape do
  def describe(shape)
end

IO.puts(Shapes.Circle.area(%Shapes.Circle{radius: 2}))
