function Get-Square {
    param($x)
    return $x * $x
}

function Get-Area {
    param($r)
    return 3.14159 * (Get-Square $r)
}

class Circle {
    [double] $Radius

    [double] Area() {
        return Get-Area $this.Radius
    }
}

Write-Host (Get-Area 2)
