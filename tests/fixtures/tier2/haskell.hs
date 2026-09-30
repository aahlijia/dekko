module Shapes where

data Circle = Circle {radius :: Double}

newtype Label = Label String

class Shape a where
  area :: a -> Double

instance Shape Circle where
  area c = pi * square (radius c)

square :: Double -> Double
square x = x * x

describe :: Int -> String
describe 0 = "none"
describe n = render (square (fromIntegral n))
  where
    render v = show v

main :: IO ()
main = putStrLn (describe 2)
