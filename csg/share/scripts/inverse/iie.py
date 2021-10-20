#!/usr/bin/env python3
"""Multi purpose script for Iterative Integral Equation methods."""
#
# Copyright 2009-2021 The VOTCA Development Team (http://www.votca.org)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Symbols:
# g: RDF
# U: potential
# dU: potential update (U_{k+1} - U_k)
#
# suffixes:
# _cur: current (of step k if currently doing iteration k)
# _tgt: target
# _ce: core_end (where RDF becomes > 0)
# _co: cut_off
# _sc: single_component
#
# prefixes:
# ndx_: index


import argparse
import itertools
import sys
import xml.etree.ElementTree as ET
try:
    import numpy as np
except ImportError:
    print("Numpy is not installed, but needed for the iterative integral equation "
          "methods.")
    raise
if not sys.version_info >= (3, 5):
    raise Exception("This script needs Python 3.5+.")
from csg_functions import (
    readin_table, saveto_table, calc_grid_spacing, fourier, fourier_all,
    gen_beadtype_property_array, gen_fourier_matrix, find_after_cut_off_ndx,
    r0_removal, get_non_bonded, get_density_dict, get_n_intra_dict,
    gen_interaction_matrix, gen_interaction_dict, gauss_newton_constrained,
    gen_flag_isfinite, kron_2D, extrapolate_dU_left_constant, vectorize, devectorize,
    if_verbose_dump_io, make_matrix_2D, make_matrix_4D, cut_matrix_inverse
)


BAR_PER_MD_PRESSURE = 16.6053904
np.seterr(all='raise')


def main():
    # get command line arguments
    args = get_args()

    # process and prepare input
    input_arrays, settings = process_input(args)

    # guess potential from distribution
    if settings['subcommand'] == 'potential_guess':
        output_arrays = potential_guess(input_arrays, settings,
                                        verbose=settings['verbose'])
        if settings['out_tgt_dcdh'] is not None:
            calc_and_save_dcdh(input_arrays, settings,
                               verbose=settings['verbose'])
    # newton update
    if settings['subcommand'] in ('newton', 'newton-mod'):
        output_arrays = newton_update(input_arrays, settings,
                                      verbose=settings['verbose'])

    # gauss-newton update
    if settings['subcommand'] == 'gauss-newton':
        output_arrays = gauss_newton_update(input_arrays, settings,
                                            verbose=settings['verbose'])

    # save output (U or dU) to table files
    save_tables(output_arrays, settings)


def get_args(iie_args=None):
    """Define and parse command line arguments.

    If iie_args is given, parse them instead of cmdlineargs.
    """
    description = "Calculate U or ΔU with Integral Equations."
    parser = argparse.ArgumentParser(description=description)
    # subparsers
    subparsers = parser.add_subparsers(dest='subcommand')
    parser_pot_guess = subparsers.add_parser(
        'potential_guess',
        help='potential guess from inverting integral equation')
    parser_newton = subparsers.add_parser(
        'newton',
        help='potential update using Newton method')
    parser_newton_mod = subparsers.add_parser(
        'newton-mod',
        help='potential update using a modified Newton method')
    parser_gauss_newton = subparsers.add_parser(
        'gauss-newton',
        help='potential update using Gauss-Newton method')
    # all subparsers
    for pars in [parser_pot_guess, parser_newton, parser_newton_mod,
                 parser_gauss_newton]:
        pars.add_argument('-v', '--verbose', dest='verbose',
                          help='save some intermeditary results',
                          action='store_const', const=True, default=False)
        pars.add_argument('--closure', type=str, choices=['hnc', 'py'],
                          required=True,
                          help='Closure equation to use for the OZ equation')
        pars.add_argument('--volume', type=float,
                          required=True,
                          metavar='VOL',
                          help='the volume of the box')
        pars.add_argument('--topol', type=argparse.FileType('r'),
                          required=True,
                          metavar='TOPOL',
                          help='XML topology file')
        pars.add_argument('--options', type=argparse.FileType('r'),
                          required=True,
                          metavar='SETTINGS',
                          help='XML settings file')
        pars.add_argument('--g-tgt-ext', type=str,
                          required=True,
                          metavar='RDF_TGT_EXT',
                          help='extension of RDF target files')
        pars.add_argument('--out-ext', type=str,
                          required=True,
                          metavar='U_OUT_EXT',
                          help='extension of U or ΔU output files')
        pars.add_argument('--g-tgt-intra-ext', type=str,
                          metavar='RDF_TGT_INTRA_EXT',
                          help='extension of intramol. RDF target files')
    # potential guess subparser
    parser_pot_guess.add_argument('--out-tgt-dcdh', type=str, default='none',
                                  help=("generate .npz file with dc/dh from target "
                                        "distributions. If 'none' it will not be "
                                        "generated."))
    # update potential subparsers
    for pars in [parser_newton, parser_newton_mod, parser_gauss_newton]:
        pars.add_argument('--g-cur-ext', type=str,
                          required=True,
                          metavar='RDF_CUR_EXT',
                          help='extension of current RDF files')
        pars.add_argument('--g-cur-intra-ext', type=str,
                          metavar='RDF_CUR_INTRA_EXT',
                          help='extension of current intramol. RDF files')
        pars.add_argument('--tgt-dcdh', type=str, default='none',
                          help=(".npz file with dc/dh from target distributions. "
                                "If provided, will be used. "
                                "If 'none' the jacobian will be calculated from "
                                "current distributions."))
    # Newton's method only options
    for pars in [parser_newton, parser_newton_mod]:
        pars.add_argument('--cut-jacobian', dest='cut_jacobian', action='store_true',
                          help=('Cut and use the top-left part of the Jacobian before'
                                + ' multiplying with Δg.'))
        pars.set_defaults(cut_jacobian=False)
    # HNCGN only options
    parser_gauss_newton.add_argument('--pressure-constraint',
                                     dest='pressure_constraint',
                                     type=str, default=None)
    # parse
    if iie_args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(iie_args)
    # check for subcommand
    if args.subcommand is None:
        parser.print_help()
        raise Exception("subcommand needed")
    return args


def process_input(args):
    """Process arguments and perform some checks."""
    # args.options.read() can be called only once
    options = ET.fromstring(args.options.read())
    topology = ET.fromstring(args.topol.read())
    # get density_dict and n_intra_dict
    density_dict = get_density_dict(topology, args.volume)
    n_intra_dict = get_n_intra_dict(topology)
    # get non_bonded_dict
    non_bonded_dict = {nb_name: nb_ts for nb_name, nb_ts in get_non_bonded(options)}
    non_bonded_dict_inv = {v: k for k, v in non_bonded_dict.items()}
    if len(non_bonded_dict) != len(non_bonded_dict_inv):
        raise Exception("Some non-bonded name was not unique or some non-bonded "
                        "interactions had the same bead types.")
    # dict of table extensions
    table_infos = {
        'g_tgt': {'extension': args.g_tgt_ext, 'check-grid': True},
    }
    # if potential guess or update and tgt_jacobian we need the target intramolecular
    # RDFs
    if args.subcommand == 'potential_guess':
        table_infos = {
            **table_infos,
            'G_minus_g_tgt': {'extension': args.g_tgt_intra_ext, 'check-grid': True},
        }
    # if update, we need the current RDFs
    if args.subcommand in ('newton', 'gauss-newton'):
        table_infos = {
            **table_infos,
            'g_cur': {'extension': args.g_cur_ext, 'check-grid': True},
        }
        # if not target jacobian we need the current intramolecular RDFs
        if args.tgt_dcdh == 'none':
            table_infos = {
                **table_infos,
                'G_minus_g_cur': {'extension': args.g_cur_intra_ext,
                                  'check-grid': True},
            }
    # load input arrays
    input_arrays = {}  # will hold all input data
    for table_name, table_info in table_infos.items():
        input_arrays[table_name] = {}
        for non_bonded_name in non_bonded_dict.keys():
            if table_info['extension'] is None:
                raise Exception(f"No file extension for {table_name} provided!")
            x, y, flag = readin_table(non_bonded_name + table_info['extension'])
            input_arrays[table_name][non_bonded_name] = {'x': x, 'y': y, 'flag': flag}
    # check for same grid and define r
    r = None
    for table_name, table_info in table_infos.items():
        for non_bonded_name in non_bonded_dict.keys():
            x = input_arrays[table_name][non_bonded_name]['x']
            if table_info['check-grid']:
                if r is None:
                    # set first r
                    r = x
                else:
                    # compare with first r
                    if not np.allclose(x, r):
                        raise RuntimeError("Grids of tables do not match")
    # check if starts at r = 0.0, if so: remove
    all_first_x = np.array([input_arrays[table_name][non_bonded_name]['x'][0]
                            for table_name, table_info in table_infos.items()
                            for non_bonded_name in non_bonded_dict.keys()])
    # if all r[0] = 0
    if np.allclose(all_first_x, np.zeros_like(all_first_x)):
        for table_name, table_info in table_infos.items():
            for non_bonded_name in non_bonded_dict.keys():
                for key in ('x', 'y', 'flag'):
                    input_arrays[table_name][non_bonded_name][key] = (
                        input_arrays[table_name][non_bonded_name][key][1:])
        r = r[1:]
        r0_removed = True
    # if they do not start with 0 but are all the same value
    elif np.allclose(all_first_x, all_first_x[0]):
        r0_removed = False
    else:
        raise Exception('either all or no input tables should start at r=0')
    # quick access to r
    input_arrays['r'] = r
    # process input further
    rhos = gen_beadtype_property_array(density_dict, non_bonded_dict)
    n_intra = gen_beadtype_property_array(n_intra_dict, non_bonded_dict)
    # settings
    # copy some directly from args
    settings_to_copy = ('closure', 'verbose', 'out_ext', 'g_extrap_factor',
                        'subcommand', 'tgt_jacobian', 'cut_jacobian')
    settings = {key: vars(args)[key] for key in settings_to_copy if key in vars(args)}
    settings['non-bonded-dict'] = non_bonded_dict
    settings['rhos'] = rhos
    settings['n_intra'] = n_intra
    settings['r0-removed'] = r0_removed
    # others from options xml
    settings['kBT'] = float(options.find("./inverse/kBT").text)
    # determine cut-off and dc/dh buffer
    if args.subcommand == 'potential_guess':
        settings['cut_off'] = float(
            options.find("./inverse/initial_guess/ie/cut_off").text)
        # dc/dh filename to write to
        if args.out_tgt_dcdh.lower() == 'none':
            settings['out_tgt_dcdh'] = None
        else:
            settings['out_tgt_dcdh'] = args.out_tgt_dcdh
    else:
        settings['cut_off'] = float(
            options.find("./inverse/iie/cut_off").text)
        # reuse dc/dh
        if args.tgt_dcdh.lower() == 'none':
            settings['tgt_dcdh'] = None
        else:
            try:
                settings['tgt_dcdh'] = np.load(args.tgt_dcdh)['dcdh']
            except (FileNotFoundError, ValueError):
                raise Exception("Can not load tgt_dcdh file that was provided")
    # determine slices from cut_off
    cut, tail = calc_slices(r, settings['cut_off'], settings['verbose'])
    settings['cut'] = cut
    settings['tail'] = tail
    return input_arrays, settings


@if_verbose_dump_io
def potential_guess(input_arrays, settings, verbose=False):
    """Calculate potential guess based on symmetry adapted RISM-OZ and closure.

    Args:
        input_arrays: nested dict holding the distributions
        settings: dict holding relevant settings
        verbose: save parameters and return of this and contained functions as numpy
                 file

    Returns:
        dictionary of potentials including flags to be saved
    """
    # obtain r
    r = input_arrays['r']
    # prepare matrices
    g_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                   settings['non-bonded-dict'])
    h_mat = g_mat - 1
    k, h_hat_mat = fourier_all(r, h_mat)
    G_minus_g_mat = gen_interaction_matrix(
        r, input_arrays['G_minus_g_tgt'], settings['non-bonded-dict'])
    _, G_minus_g_hat_mat = fourier_all(r, G_minus_g_mat)
    # perform actual math
    U_mat = calc_U_matrix(r, k, g_mat, h_hat_mat, G_minus_g_hat_mat,
                          settings['rhos'], settings['n_intra'],
                          settings['kBT'], settings['closure'],
                          verbose=settings['verbose'])
    # extrapolate and save potentials
    output_arrays = {}
    for non_bonded_name, U_dict in gen_interaction_dict(
            r, U_mat, settings['non-bonded-dict']).items():
        U = U_dict['y']
        U_flag = gen_flag_isfinite(U)
        # make tail zero. It is spoiled on the last half from inverting OZ.
        # careful: slices refer to arrays before reinserting r=0 values!
        cut, tail = settings['cut'], settings['tail']
        U[cut] -= U[cut][-1]
        U[tail] = 0
        U_flag[tail] = 'o'
        # reinsert r=0 values
        if settings['r0-removed']:
            r_out = np.concatenate(([0.0], r))
            U = np.concatenate(([np.nan], U))
            U_flag = np.concatenate((['o'], U_flag))
        else:
            r_out = r
        # change NaN in the core region to first valid value
        U = extrapolate_dU_left_constant(U, U_flag)
        output_arrays[non_bonded_name] = {'x': r_out, 'y': U, 'flag': U_flag}
    return output_arrays


def save_tables(output_arrays, settings):
    """Save each entry in output_arrays to a table file."""
    comment = "created by: {}".format(" ".join(sys.argv))
    if settings['out_ext'].lower() == 'none':
        return None
    for non_bonded_name, output_dict in output_arrays.items():
        fn = non_bonded_name + settings['out_ext']
        saveto_table(fn, output_dict['x'], output_dict['y'], output_dict['flag'],
                     comment)


@if_verbose_dump_io
def calc_U_matrix(r, k, g_mat, h_hat_mat, G_minus_g_hat_mat, rhos, n_intra, kBT,
                  closure, verbose=False):
    """
    Calculate a potential U using integral equation theory.

    Args:
        r: Distance grid.
        g_mat: matrix of RDF
        h_hat_mat: matrix of Fourier of TCF
        G_minus_g_mat: matrix of Fourier of intramolecular RDF
        rhos: array of densities of the bead types
        n_intra: array with number of bead per molecule
        kBT: Boltzmann constant times temperature.
        closure: OZ-equation closure ('hnc' or 'py').
        verbose: output calc_U_matrix.npz

    Returns:
        matrix of the calculated potentias.
    """
    # calculate direct correlation function
    c_mat = calc_c_matrix(r, k, h_hat_mat, G_minus_g_hat_mat, rhos, n_intra, verbose)
    with np.errstate(divide='ignore', invalid='ignore'):
        if closure == 'hnc':
            U_mat = kBT * (-np.log(g_mat) + (g_mat - 1) - c_mat)
        elif closure == 'py':
            U_mat = kBT * np.log(1 - c_mat/g_mat)
    return U_mat


@if_verbose_dump_io
def calc_c_matrix(r, k, h_hat_mat, G_minus_g_hat_mat, rhos, n_intra, verbose=False):
    """Calculate the direct correlation function c from g for all interactions."""
    # row sum of ω, after Bertagnolli and my own notes
    Omega_hat_mat = gen_Omega_hat_mat(G_minus_g_hat_mat, rhos, n_intra)
    # H_hat_mat after Bertagnolli
    H_hat_mat = adapt_reduced_matrix(h_hat_mat, n_intra)
    # Rho_mat after Bertagnolli
    Rhos = rhos / n_intra
    # intermediate step
    # have to transpose to solve x·a = b with numpy by solving a'·x' = b'
    H_over_Omega_plus_rho_H = transpose(np.linalg.solve(
        transpose(Omega_hat_mat + np.diag(Rhos) @ H_hat_mat),
        transpose(H_hat_mat)))
    # direct correlation function C from symmetry reduced OZ
    C_hat_mat = np.linalg.solve(Omega_hat_mat, H_over_Omega_plus_rho_H)
    # c_hat from C_hat
    c_hat_mat = unadapt_reduced_matrix(C_hat_mat, n_intra)
    # c from c_hat
    _, c_mat = fourier_all(k, c_hat_mat)
    return c_mat


@if_verbose_dump_io
def newton_update(input_arrays, settings, verbose=False):
    """Calculate Newton potential update based on symmetry adapted RISM-OZ and closure.

    Args:
        input_arrays: nested dict holding the distributions
        settings: dict holding relevant settings
        verbose: save parameters and return of this and contained functions as numpy
                 file

    Returns:
        dictionary of potential updates including flags to be saved
    """
    # obtain r
    r = input_arrays['r']
    # number of atom types
    n_t = len(settings['rhos'])
    # number of interactions including redundand ones
    n_i = int(n_t**2)
    # number of grid points in r
    n_r = len(r)
    # slices
    cut, _ = settings['cut'], settings['tail']
    # generate matrices
    g_tgt_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                       settings['non-bonded-dict'])
    g_cur_mat = gen_interaction_matrix(r, input_arrays['g_cur'],
                                       settings['non-bonded-dict'])
    # calculate the ready-to-use jacobian inverse
    _, jac_inv_mat = calc_jacobian(input_arrays, settings, verbose)
    # Delta g for potential update
    Delta_g_mat = g_cur_mat - g_tgt_mat
    # vectorize Delta g
    Delta_g_vec = vectorize(Delta_g_mat)
    # prepare potential update array
    dU_vec = np.zeros((n_r, n_i))
    with np.errstate(invalid='ignore'):
        for h, (i, j) in enumerate(itertools.product(range(n_i), range(n_i))):
            # Newton update
            dU_vec[cut, i] -= (jac_inv_mat[cut, cut, i, j] @ Delta_g_vec[cut, j])
    # dU matrix
    dU_mat = devectorize(dU_vec)
    # prepare output
    output_arrays = {}
    for non_bonded_name, dU_dict in gen_interaction_dict(
            r, dU_mat, settings['non-bonded-dict']).items():
        dU = dU_dict['y']
        dU_flag = gen_flag_isfinite(dU)
        if settings['r0-removed']:
            r_out = np.concatenate(([0.0], r))
            dU = np.concatenate(([np.nan], dU))
            dU_flag = np.concatenate((['o'], dU_flag))
        else:
            r_out = r
        # shift potential to make last value zero
        dU -= dU[-1]
        # change NaN in the core region to first valid value
        dU = extrapolate_dU_left_constant(dU, dU_flag)
        # save for output
        output_arrays[non_bonded_name] = {'x': r_out, 'y': dU, 'flag': dU_flag}
    return output_arrays


@if_verbose_dump_io
def calc_jacobian(input_arrays, settings, verbose=False):
    """
    Calculate dg/du, the Jacobian and its inverse du/dg using RISM-OZ + closure.

    Args:
        input_arrays: nested dict holding the distributions
        settings: dict holding relevant settings
        verbose: save parameters and return of this and contained functions as numpy
                 file

    Returns:
        The Jacobian™ and its inverse
    """
    # obtain r
    r = input_arrays['r']
    # number of atom types
    n_t = len(settings['rhos'])
    # number of interactions
    n_i = int(n_t**2)
    # number of grid points in r
    n_r = len(r)
    # slices
    cut, _ = settings['cut'], settings['tail']
    n_c = len(r[cut])
    # generate matrices
    g_tgt_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                       settings['non-bonded-dict'])
    g_cur_mat = gen_interaction_matrix(r, input_arrays['g_cur'],
                                       settings['non-bonded-dict'])
    # wether the jacobian will be modified to imitate the ibi-update in the first term
    newton_mod = settings['subcommand'] == 'newton-mod'
    # which distributions to use for dc/dh
    # using cur is the original Newton-Raphson root finding method
    # using tgt is a method similar to Newton's but with slope calculated at the root
    # the latter is taken from the input, is calculated at step_000 once
    if settings['tgt_dcdh'] is not None:
        dcdh = settings['tgt_dcdh']
        # the input dc/dh should already be cut to the cut-off
        assert n_c == dcdh.shape[0]
    else:
        # generate dc/dh, invert, cut it, and invert again
        G_minus_g_cur_mat = gen_interaction_matrix(r, input_arrays['G_minus_g_cur'],
                                                   settings['non-bonded-dict'])
        # calculate dc/dh on long range
        dcdh_long = calc_dcdh(r, g_cur_mat, G_minus_g_cur_mat,
                              settings['rhos'], settings['n_intra'],
                              settings['kBT'], verbose)
        dcdh_long_2D = make_matrix_2D(dcdh_long)
        # cut invert dc/dh, cut dh/dc, invert again
        dcdh_2D = cut_matrix_inverse(dcdh_long_2D, n_r, n_i, cut)
        # make it a 4D array again
        dcdh = make_matrix_4D(dcdh_2D, n_c, n_i)
    # add the 1/g term to dc/dh and obtain inverse Jacobian
    jac_inv_mat = add_jac_inv_diagonal(r[cut], g_tgt_mat[cut], g_cur_mat[cut],
                                       dcdh, settings['rhos'],
                                       settings['n_intra'], settings['kBT'],
                                       settings['closure'], newton_mod, verbose)
    jac_mat = np.linalg.inv(jac_inv_mat)
    return jac_mat, jac_inv_mat


@if_verbose_dump_io
def calc_dcdh(r, g_mat, G_minus_g_mat, rhos, n_intra, kBT, verbose=False):
    """
    Calculate the derivative dvec(c)/dvec(h) which is part of the Jacobian.

    Args:
        r: Distance grid
        g_mat: matrix of RDF (target or current)
        G_minus_g_cur_mat: matrix of intramolecular RDF (target or current)
        rhos: Number densities of the beads
        n_intra: Number of equal beads per molecule

    Returns:
        The inverse jacobian
    """
    # number of atom types
    n_t = len(rhos)
    # number of interactions
    n_i = int(n_t**2)
    # FT of total correlation function 'h' and G_minus_g
    k, h_hat_mat = fourier_all(r, g_mat - 1)
    _, G_minus_g_hat_mat = fourier_all(r, G_minus_g_mat)
    # Fourier matrix
    F = gen_fourier_matrix(r, fourier)
    F_inv = np.linalg.inv(F)
    # Ω
    Omega_hat_mat = gen_Omega_hat_mat(G_minus_g_hat_mat, rhos, n_intra)
    # ρ molecular, entries are mol densities as needed by symmetry adapted rism
    rho_mol_map = np.diag(rhos / n_intra)  # rho molecular
    # I, identity matrix
    identity = np.identity(n_t)
    # symmetry adapt h -> H
    H_hat_mat = adapt_reduced_matrix(h_hat_mat, n_intra)
    # version derived from vectorizing Martin Hankes result
    A = np.swapaxes(np.linalg.inv(Omega_hat_mat + rho_mol_map @ H_hat_mat), -1, -2)
    B = np.linalg.inv(Omega_hat_mat) @ (identity - H_hat_mat @ np.linalg.inv(
        Omega_hat_mat + rho_mol_map @ H_hat_mat) @ rho_mol_map)
    d_vec_c_hat_by_d_vec_h_hat = kron_2D(A, B)
    # now it becomes an operator by diag and applying Fourier
    d_vec_c_by_d_vec_h = np.zeros((len(r), len(r), n_i, n_i))
    for h, (i, j) in enumerate(itertools.product(range(n_i), range(n_i))):
        d_vec_c_by_d_vec_h[:, :, i, j] = (F_inv
                                          @ np.diag(d_vec_c_hat_by_d_vec_h_hat[:, i, j])
                                          @ F)
    return d_vec_c_by_d_vec_h


@if_verbose_dump_io
def calc_and_save_dcdh(input_arrays, settings, verbose=False):
    """
    Calculate dc/dh in its cut form and save it

    Args:
        input_arrays: nested dict holding the distributions
        settings: dict holding relevant settings
        verbose: save parameters and return of this and contained functions as numpy
                 file

    Returns:
        dc/dh on the cut range
    """
    # obtain r
    r = input_arrays['r']
    # number of atom types
    n_t = len(settings['rhos'])
    # number of interactions
    n_i = int(n_t**2)
    # number of grid points in r
    n_r = len(r)
    # slices
    cut, _ = settings['cut'], settings['tail']
    n_c = len(r[cut])
    # generate matrices
    g_tgt_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                       settings['non-bonded-dict'])
    # generate dc/dh, invert, cut it, and invert again
    G_minus_g_tgt_mat = gen_interaction_matrix(r, input_arrays['G_minus_g_tgt'],
                                               settings['non-bonded-dict'])
    # calculate dc/dh on long range
    dcdh_long = calc_dcdh(r, g_tgt_mat, G_minus_g_tgt_mat,
                          settings['rhos'], settings['n_intra'],
                          settings['kBT'], verbose)
    dcdh_long_2D = make_matrix_2D(dcdh_long)
    # cut invert dc/dh, cut dh/dc, invert again
    dcdh_2D = cut_matrix_inverse(dcdh_long_2D, n_r, n_i, cut)
    # make it a 4D array again
    dcdh = make_matrix_4D(dcdh_2D, n_c, n_i)
    # save to npz file
    np.savez_compressed(settings['out_tgt_dcdh'], dcdh=dcdh)


@if_verbose_dump_io
def add_jac_inv_diagonal(r, g_tgt_mat, g_cur_mat, dcdh, rhos, n_intra, kBT,
                         closure, newton_mod, tgt_dcdh=None, verbose=False):
    """
    Calculate du/dg, the inverse of the Jacobian.

    Args:
        r: Distance grid
        g_tgt_mat: target RDFs
        g_cur_mat: current RDFs
        dcdh_2D: derivative dc/dh
        rhos: Number densities of the beads
        n_intra: Number of equal beads per molecule
        kBT: Boltzmann constant times temperature
        closure: OZ-equation closure ('hnc' or 'py')
        newton_mod: Use IBI style update term

    Returns:
        Matrix inverse of the Jacobian
    """
    # number of atom types
    n_t = len(rhos)
    # number of interactions
    n_i = int(n_t**2)
    # vectorize RDF matrices
    g_tgt_vec = vectorize(g_tgt_mat)
    g_cur_vec = vectorize(g_cur_mat)
    # average RDF for better stability
    g_avg_vec = (g_tgt_vec + g_cur_vec) / 2
    if closure == 'hnc':
        jac_inv_mat = np.zeros((len(r), len(r), n_i, n_i))
        # BEGIN TODO
        if newton_mod:
            # old code for single bead systems
            """with np.errstate(divide='ignore', invalid='ignore', under='ignore'):
                jac_inv1 = kBT * (1 + np.log(g_tgt / g_cur) / Delta_g)
            jac_inv2 = -kBT * dcdg
            # Some fixes, because we want to define a jacobian matrix
            # Unmodified Newton is less awkward
            # Ensure this is zero, not nan, on the diagonal where Delta_g is zero
            jac_inv1[Delta_g == 0] = 0
            # Ensure this is -np.inf, not nan, on the diagonal in the core region
            jac_inv1[:nocore.start] = -np.inf
            jac_inv = np.diag(jac_inv1) + jac_inv2"""
            raise NotImplementedError
        # END TODO
        else:
            with np.errstate(divide='ignore', invalid='ignore', under='ignore'):
                for h, (i, j) in enumerate(itertools.product(range(n_i), range(n_i))):
                    if i == j:
                        diagonal_term = np.diag(1 - 1 / g_avg_vec[:, i])
                    else:
                        diagonal_term = 0
                    jac_inv_mat[:, :, i, j] = kBT * (diagonal_term - dcdh[:, :, i, j])
    elif closure == 'py':
        raise NotImplementedError
    return jac_inv_mat


def gauss_newton_update(r, input_arrays, args):
    """Do the Gauss-Newton update."""
    # parse constraints
    constraints = []
    if args.pressure_constraint is not None:
        p_target = float(args.pressure_constraint.split(',')[0])
        p_current = float(args.pressure_constraint.split(',')[1])
        constraints.append({'type': 'pressure', 'target': p_target,
                            'current': p_current})

    # calc dU_pure
    dU_pure = calc_dU_gauss_newton(r,
                                   input_arrays['g_tgt'][0]['y'],
                                   input_arrays['g_cur'][0]['y'],
                                   input_arrays['G_minus_g'][0]['y'],
                                   args.n_intra[0], args.kBT,
                                   args.densities[0], args.cut_off,
                                   constraints, verbose=args.verbose)

    # set dU_flag to 'o' inside the core
    dU_flag = np.where(np.isnan(dU_pure), 'o', 'i')

    # select extrapolation
    if args.extrap_near_core == 'none':
        dU_extrap = np.nan_to_num(dU_pure)
    elif args.extrap_near_core == 'constant':
        # dU_extrap = extrapolate_U_constant(dU_pure, dU_flag)
        pass
    else:
        raise Exception("unknown extrapolation scheme for inside and near "
                        "core region: " + args.extrap_near_core)
    # shifts to correct potential after cut-off
    dU_shift = shift_U_cutoff_zero(dU_extrap, r,
                                   input_arrays['U_cur'][0]['y'],
                                   args.cut_off)
    # shifts to correct potential near cut-off
    if args.fix_near_cut_off == 'none':
        dU = dU_shift.copy()
    elif args.fix_near_cut_off == 'full-deriv':
        U_new = input_arrays['U_cur'][0]['y'] + dU_shift
        # U_new = fix_U_near_cut_off_full(r, U_new, args.cut_off)
        dU = U_new - input_arrays['U_cur'][0]['y']
    else:
        raise Exception("unknown fix scheme for near cut-off: "
                        + args.fix_near_cut_off)

    if args.verbose:
        np.savez_compressed('hncgn-dU.npz', r=r, dU_pure=dU_pure,
                            dU_extrap=dU_extrap, dU_shift=dU_shift)
    comment = "created by: {}".format(" ".join(sys.argv))
    saveto_table(args.U_out[0], r, dU, dU_flag, comment)


def gen_Omega_hat_mat(G_minus_g_hat_mat, rhos, n_intra):
    # σ is any row sum of ω
    sigma_R = G_minus_g_hat_mat @ np.diag(rhos) + np.identity(len(rhos))
    # weighting of row sum σ
    Omega_hat_mat = np.diag(np.sqrt(n_intra)) @ sigma_R @ np.diag(1/np.sqrt(n_intra))
    return Omega_hat_mat


def adapt_reduced_matrix(mat, n_intra):
    """Adapt the prefactors of a matrix to be compatible with the symmetry reduced RISM
    equation.

    The input matrix is already reduced (rows are atom types not atoms), but factors
    need to be applied."""
    Mat = np.diag(np.sqrt(n_intra)) @ mat @ np.diag(np.sqrt(n_intra))
    return Mat


def unadapt_reduced_matrix(Mat, n_intra):
    """Unadapt the prefactors of a matrix compatible with the symmetry reduced RISM
    equation back to the regular form.

    The input matrix is already reduced (rows are atom types not atoms) and adapted.
    Factors are applied to get back to the regular matrix."""
    mat = np.diag(1/np.sqrt(n_intra)) @ Mat @ np.diag(1/np.sqrt(n_intra))
    return mat


def transpose(mat):
    """First dimension is radius or k. Transpose means swapping the last two axis."""
    return np.swapaxes(mat, -1, -2)


def calc_slices(r, cut_off, verbose=False):
    """
    Generate slices for the regions used in the IIE methods.

    There are different regions used:
    |        cut             |       tail       |  # regions
    0---------------------cut_off-----------r[-1]  # distances
    0---------------------ndx_co---------len(r)+1  # indices
    note: in earlier versions, there were slices (nocore, crucial) that
    excluded the core region
    """
    ndx_co = find_after_cut_off_ndx(r, cut_off)
    cut = slice(0, ndx_co)
    tail = slice(ndx_co, None)
    if verbose:
        print("ndx_co: {}, ({})".format(ndx_co, cut_off))
        print("min(r): {}".format(min(r)))
        print("max(r): {}".format(max(r)))
        print("len(r): {}".format(len(r)))
        print("cut:", cut.start, cut.stop, min(r[cut]), max(r[cut]))
        if len(r[tail]) > 0:
            print("tail:", tail.start, tail.stop, min(r[tail]), max(r[tail]))
    return cut, tail


def calc_dU_gauss_newton(r, g_tgt, g_cur, G_minus_g, n, kBT, rho,
                         cut_off, constraints,
                         verbose=False):
    """
    Calculate a potential update dU using the Gauss-Newton method.

    Constraints can be added.

    Args:
        r: Distance grid.
        g_tgt: Target RDF.
        g_cur: Current RDF.
        kBT: Boltzmann constant times temperature.
        rho: Number density of the molecules.
        cut_off: Highest distance for potential update.
        constraints: List of dicts, which describe physical constraints.

    Returns:
        The calculated potential update.

    """
    r0_removed, (r, g_tgt, g_cur, G_minus_g) = r0_removal(r, g_tgt, g_cur, G_minus_g)
    # calc slices and Delta_r
    nocore, crucial = calc_slices(r, g_tgt, g_cur, cut_off, verbose=verbose)
    Delta_r = calc_grid_spacing(r)
    # pair correlation function 'h'
    h = g_cur - 1
    # special Fourier of h
    _, h_hat = fourier(r, h)
    # Fourier matrix
    F = gen_fourier_matrix(r, fourier)
    # dc/dg
    if n == 1:
        # single bead case
        dcdg = np.linalg.inv(F) @ np.diag(1 / (1 + rho * h_hat)**2) @ F
    else:
        _, G_minus_g_hat = fourier(r, G_minus_g)
        dcdg = np.linalg.inv(F) @ np.diag(1 / (1 + n * rho * G_minus_g_hat
                                               + n * rho * h_hat)**2) @ F
    # jacobian^-1 (matrix U in Delbary et al., with respect to potential)
    with np.errstate(divide='ignore', invalid='ignore', under='ignore'):
        jac_inv = kBT * (np.diag(1 - 1 / g_cur[nocore]) - dcdg[nocore, nocore])
    # A0 matrix
    A0 = Delta_r * np.triu(np.ones((len(r[nocore]), len(r[crucial])-1)), k=0)
    # Jacobian with respect to force
    J = np.linalg.inv(jac_inv) @ A0
    # constraint matrix and vector
    C = np.zeros((len(constraints), len(r[crucial])-1))
    d = np.zeros(len(constraints))
    # build constraint matrix and vector from constraints
    if verbose:
        print(constraints)
    for c, constraint in enumerate(constraints):
        if constraint['type'] == 'pressure':
            # current pressure
            p = constraint['current'] / BAR_PER_MD_PRESSURE
            # target pressure
            p_tgt = constraint['target'] / BAR_PER_MD_PRESSURE
            # g_tgt(r_{i+1})
            g_tgt_ip1 = g_tgt[crucial][1:]
            # g_tgt(r_{i})
            g_tgt_i = g_tgt[crucial][:-1]
            # r_{i+1}
            r_ip1 = r[crucial][1:]
            # r_{i}
            r_i = r[crucial][:-1]
            # l vector
            ll = (g_tgt_i + g_tgt_ip1) * (r_ip1**4 - r_i**4)
            ll *= 1/12 * np.pi * rho**2
            # set C row and d element
            C[c, :] = ll
            d[c] = p_tgt - p
        else:
            raise Exception("not implemented constraint type")
    # residuum vector
    res = g_tgt - g_cur
    # switching to notation of Gander et al. for solving
    A = J
    b = res[nocore]
    w = gauss_newton_constrained(A, C, b, d)
    # dU
    dU = A0 @ w
    # fill core with nans
    dU = np.concatenate((np.full(nocore.start, np.nan), dU))
    # dump files
    if verbose:
        np.savez_compressed('gauss-newton-arrays.npz', A=A, b=b, C=C, d=d,
                            jac_inv=jac_inv, A0=A0, J=J)
    if r0_removed:
        dU = np.concatenate(([np.nan], dU))
    return dU


def shift_U_cutoff_zero(dU, r, U, cut_off):
    """Make potential zero at and beyond cut-off."""
    dU_shift = dU.copy()
    # shift dU to be zero at cut_off and beyond
    ndx_co = find_after_cut_off_ndx(r, cut_off)
    U_before_cut_off = U[ndx_co-1] + dU[ndx_co-1]
    dU_shift -= U_before_cut_off
    dU_shift[ndx_co:] = -1 * U[ndx_co:]
    return dU_shift


if __name__ == '__main__':
    main()
