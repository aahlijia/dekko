namespace eval ::shapes {
    proc square {x} {
        return [expr {$x * $x}]
    }

    proc area {r} {
        return [expr {3.14159 * [square $r]}]
    }
}

oo::class create Circle {
    method describe {r} {
        return [::shapes::area $r]
    }
}

proc ::shapes::report {r} {
    puts [area $r]
}

::shapes::report 2
