with Ada.Text_IO;

package body Shapes is

   function Square (X : Float) return Float is
   begin
      return X * X;
   end Square;

   function Area (Radius : Float) return Float is
   begin
      return 3.14159 * Square (Radius);
   end Area;

   procedure Describe (Radius : Float) is
   begin
      Ada.Text_IO.Put_Line (Format (Area (Radius)));
   end Describe;

end Shapes;
