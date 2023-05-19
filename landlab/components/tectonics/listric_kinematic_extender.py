#!/usr/bin/env python3
"""
Apply tectonic extension and subsidence kinematically.

Landlab component that simulates development of an asymmetric rift on a listric
fault plane.

See notebook tutorial for complete examples.

@author: gtucker
"""

import numpy as np

from landlab import Component, HexModelGrid, RasterModelGrid


class ListricKinematicExtender(Component):
    """Apply tectonic extension and subsidence kinematically to a raster or
    hex grid.

    Extension is east-west, with a north-south fault in the case of a raster
    grid, and a N30E fault in the case of a hex grid (obique extension).

    Examples
    --------
    >>> from landlab import RasterModelGrid
    >>> from landlab.components import ListricKinematicExtender
    >>> grid = RasterModelGrid((3, 7), xy_spacing=2500.0)
    >>> topo = grid.add_zeros('topographic__elevation', at='node')
    >>> lke = ListricKinematicExtender(grid, fault_location=2500.0)
    >>> lke.update_subsidence_rate()
    >>> np.round(grid.at_node["subsidence_rate"][8:13], 6)
    array([ 0.      ,  0.001123,  0.000729,  0.000472,  0.000306])
    """

    _name = "ListricKinematicExtender"

    _time_units = "y"

    _unit_agnostic = True

    _info = {
        "fault_plane__elevation": {
            "dtype": "float",
            "intent": "out",
            "optional": True,
            "units": "m",
            "mapping": "node",
            "doc": "Elevation of fault plane",
        },
        "hangingwall__thickness": {
            "dtype": "float",
            "intent": "inout",
            "optional": True,
            "units": "m",
            "mapping": "node",
            "doc": "Thickness of material in hangingwall block",
        },
        "hangingwall__velocity": {
            "dtype": "float",
            "intent": "out",
            "optional": True,
            "units": "m/y",
            "mapping": "link",
            "doc": "Horizontal velocity of material in hangingwall block",
        },
        "topographic__elevation": {
            "dtype": "float",
            "intent": "inout",
            "optional": False,
            "units": "m",
            "mapping": "node",
            "doc": "Land surface topographic elevation",
        },
    }

    def __init__(
        self,
        grid,
        extension_rate=0.001,
        fault_dip=60.0,
        fault_location=None,
        detachment_depth=1.0e4,
        track_crustal_thickness=False,
        fields_to_advect=None,
    ):
        """Deform vertically and horizontally to represent tectonic extension.

        Parameters
        ----------
        grid: RasterModelGrid
            A landlab grid.
        extension_rate: float, optional
            Rate of horizontal motion of hangingwall relative to footwall
            (m / y), default 1 mm/y.
        fault_dip: float, optional
            Dip of the fault, degrees (default 45).
        fault_location: float, optional
            Distance of fault trace from x=0 (m) (default = grid width / 2).
        detachment_depth: float, optional
            Depth to horizontal detachment (m), default 10 km.
        track_crustal_thickness: bool, optional
            Option to keep track of changes in crustal thickness (default False)
        fields_to_advect: list of str, optional
            List of names of fields, in addition to 'hangingwall__thickness'
            and (if track_crustal_thickness==True) 'upper_crust_thickness',
            that should be advected horizontally.
        """
        fields_to_shift = [] if fields_to_shift is None else fields_to_shift

        is_raster = isinstance(grid, RasterModelGrid)
        is_hex = isinstance(grid, HexModelGrid)
        if not (is_raster or is_hex):
            raise TypeError("Grid must be RasterModelGrid or HexModelGrid")
        elif isinstance(grid, HexModelGrid) and (
            grid.node_layout == "hex" or grid.number_of_node_columns % 2 == 0
        ):
            raise TypeError(
                "Hex grid must have rectangular layout & odd number of columns"
            )

        super().__init__(grid)
        self.initialize_output_fields()

        self._extension_rate = extension_rate
        self._detachment_depth = detachment_depth
        self._decay_length = detachment_depth / self._fault_grad

        self._elev = grid.at_node["topographic__elevation"]
        self._fault_plane_elev = grid.at_node["fault_plane__elevation"]

        self._fields_to_advect = fields_to_advect.copy()
        self._fields_to_advect.append("hangingwall__thickness")

        self._track_thickness = track_crustal_thickness
        if self._track_thickness:
            try:
                self._thickness = grid.at_node["upper_crust_thickness"]
            except KeyError as exc:
                raise KeyError(
                    "When handle_thickness is True you must provide an"
                    "'upper_crust_thickness' node field."
                ) from exc
            self._fields_to_advect.append("upper_crust_thickness")

    def setup_fault_plane_elevation(self, grid, fault_loc, fault_dip, detachment_depth):
        """Set up the field fault_plane__elevation"""
        if isinstance(grid, HexModelGrid):
            if fault_loc is None:
                fault_loc = 0.0
            ds = grid.spacing
            if grid.orientation[0] == "h":
                phi = np.deg2rad(-30.0)
            else:
                raise NotImplementedError(
                    "vertical orientation hex grids not currently handled"
                )
            fault_normal_coord = grid.x_of_node + grid.y_of_node * np.tan(phi)
        else:
            fault_normal_coord = grid.x_of_node
            ds = grid.dy
            if fault_loc is None:
                fault_loc = 0.5 * (
                    np.amax(self._fault_normal_coord)
                    - np.amin(self._fault_normal_coord)
                )

        fault_grad = np.tan(np.deg2rad(fault_dip))
        dist_to_fault = fault_normal_coord - fault_loc
        self._fault_plane_elev[:] = -(
            detachment_depth
            * (1.0 - np.exp(-dist_to_fault * fault_grad / detachment_depth))
        )

    def run_one_step(self, dt):
        """Apply extensional motion to grid for one time step."""
        self.update_subsidence_rate()
        self._elev[self._hangwall] -= self._subs_rate[self._hangwall] * dt
        self._horiz_displacement += self._extension_rate * dt
        if self._track_thickness:
            self._cum_subs[self._hangwall] += self._subs_rate[self._hangwall] * dt

        # Shift hangingwall nodes by one cell
        if self._horiz_displacement >= self._ds:
            for fieldname in self._fields_to_shift:
                self.grid.at_node[fieldname][self._hw_downwind] = self.grid.at_node[
                    fieldname
                ][self._hw_upwind]

            # Pull elevation along the current fault nodes down to either the
            # fault plane, or their original values, whichever is lower.
            self._elev[self._fault_nodes] = np.minimum(
                self._elev[self._fault_nodes],
                self.fault_plane_elev(
                    (self._hangwall_edge - self._fault_loc) + self._ds
                ),
            )

            # Subtract cumulative subsidence from thickness, and zero out
            # cumulative subsidence (it is subsidence since latest horiz shift)
            if self._track_thickness:
                self._thickness -= self._cum_subs
                self._cum_subs[:] = 0.0

            # Update horizontal displacement (since latest shift) and
            # hangingwall locations
            self._horiz_displacement -= self._ds
            self._hangwall_edge += self._ds
            self._update_hangingwall_nodes()
