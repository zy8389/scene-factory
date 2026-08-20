from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a local Y-up, centimeter-unit demo mug for asset-pipeline testing."
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if not str(output).isascii():
        raise ValueError("use an ASCII-only USD output path on Windows")

    from pxr import Gf, Usd, UsdGeom

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(output.as_posix())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    root = UsdGeom.Xform.Define(stage, "/DemoMug")

    body = UsdGeom.Cylinder.Define(stage, "/DemoMug/Body")
    body.CreateAxisAttr(UsdGeom.Tokens.y)
    body.CreateRadiusAttr(4.2)
    body.CreateHeightAttr(10.0)
    body.CreateDisplayColorAttr([Gf.Vec3f(0.12, 0.38, 0.72)])
    UsdGeom.Xformable(body).AddTranslateOp().Set(Gf.Vec3d(0.0, 5.0, 0.0))

    handle_color = [Gf.Vec3f(0.09, 0.28, 0.58)]
    pieces = {
        "HandleOuter": ((6.0, 5.0, 0.0), (2.0, 6.0, 2.0)),
        "HandleTop": ((4.8, 8.0, 0.0), (3.0, 2.0, 2.0)),
        "HandleBottom": ((4.8, 2.0, 0.0), (3.0, 2.0, 2.0)),
    }
    for name, (translation, scale) in pieces.items():
        cube = UsdGeom.Cube.Define(stage, f"/DemoMug/{name}")
        cube.CreateSizeAttr(1.0)
        cube.CreateDisplayColorAttr(handle_color)
        xform = UsdGeom.Xformable(cube)
        xform.AddTranslateOp().Set(Gf.Vec3d(*translation))
        xform.AddScaleOp().Set(Gf.Vec3f(*scale))

    stage.SetDefaultPrim(root.GetPrim())
    stage.GetRootLayer().Save()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
