// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IShape {
    function area() external view returns (uint256);
}

library Math2 {
    function square(uint256 x) internal pure returns (uint256) {
        return x * x;
    }
}

contract Circle is IShape {
    uint256 public radius;

    modifier positive(uint256 r) {
        require(r > 0);
        _;
    }

    function setRadius(uint256 r) external positive(r) {
        radius = r;
    }

    function area() external view returns (uint256) {
        return 3 * Math2.square(radius);
    }
}
