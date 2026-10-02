unit Shapes;

interface

type
  TCircle = class(TObject)
  public
    function Area: Double;
    procedure Describe(Scale: Integer);
  end;

implementation

function Square(X: Double): Double;
begin
  Result := X * X;
end;

function TCircle.Area: Double;
begin
  Result := 3.14159 * Square(2.0);
end;

procedure TCircle.Describe(Scale: Integer);
begin
  WriteLn(Square(Scale));
end;

end.
