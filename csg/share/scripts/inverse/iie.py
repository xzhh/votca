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
# _mat: matrix (bead, bead) in the last two dimensions
# _vec: _mat matrix with all interactions flattened (vectorized)
# _2D: 2D matrix (which can be transformed to 4D)
# _flat: flat version of a vector, corresponds to matrix with _2D
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
    get_non_bonded, get_density_dict, get_n_intra_dict, get_charge_dict,
    gen_interaction_matrix, gen_interaction_dict, solve_lsq_with_linear_constraints,
    gen_flag_isfinite, kron_2D, extrapolate_dU_left_constant, vectorize, devectorize,
    if_verbose_dump_io, make_matrix_2D, make_matrix_4D, cut_matrix_inverse, transpose,
    get_bead_types, get_intra_needed
)


BAR_PER_MD_PRESSURE = 16.6053904
F_COULOMB = 138.935458  # electric conversion factor: V = f*q_1*q_2/r
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
    # calculate dc/dh
    if settings['subcommand'] == 'dcdh':
        if settings['out'] is not None:
            calc_and_save_dcdh(input_arrays, settings,
                               verbose=settings['verbose'])
            return
    # newton update
    if settings['subcommand'] == 'newton':
        output_arrays = newton_update(input_arrays, settings,
                                      verbose=settings['verbose'])
    # gauss-newton update
    if settings['subcommand'] == 'gauss-newton':
        if settings['multistate']:
            output_arrays = multistate_gauss_newton_update(input_arrays, settings,
                                                           verbose=settings['verbose'])
        else:
            if any(settings['upd_pots'].values()):  # only update if requested
                output_arrays = gauss_newton_update(input_arrays, settings,
                                                    verbose=settings['verbose'])
            else:
                print("No potentials to update with iie gauss-newton, writing zeros.")
                output_arrays = zero_update(input_arrays, settings,
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
    parser_gauss_newton = subparsers.add_parser(
        'gauss-newton',
        help='potential update using Gauss-Newton method')
    parser_dcdh = subparsers.add_parser(
        'dcdh',
        help='calculate the dc/dh matrix')
    # all subparsers
    for pars in (parser_pot_guess, parser_newton, parser_gauss_newton, parser_dcdh):
        pars.add_argument('-v', '--verbose', dest='verbose',
                          help='save some intermeditary results',
                          action='store_const', const=True, default=False)
        pars.add_argument('--volume', type=float,
                          required=True, nargs='+', metavar='VOL',
                          help='the volume of the box. Multiple if multistate')
        pars.add_argument('--topol', type=argparse.FileType('r'),
                          required=True, nargs='+', metavar='TOPOL',
                          help='XML topology file, Multiple if multistate')
        pars.add_argument('--options', type=argparse.FileType('r'),
                          required=True, metavar='SETTINGS',
                          help='XML settings file')
        pars.add_argument('--g-tgt-ext', type=str,
                          required=True,
                          metavar='RDF_TGT_EXT',
                          help='extension of RDF target files')
        pars.add_argument('--out', type=str,
                          required=True,
                          metavar='U_OUT_EXT',
                          help="extension of U or ΔU files or full filename of dcdh "
                          "matrix. If 'none' there will be no output.")
        pars.add_argument('--g-tgt-intra-ext', type=str,
                          metavar='RDF_TGT_INTRA_EXT',
                          help='extension of intramol. RDF target files')
    # potential guess or update subparsers
    for pars in (parser_pot_guess, parser_newton, parser_gauss_newton):
        # closure not needed for dc/dh
        pars.add_argument('--closure', type=str, choices=['hnc', 'py'],
                          required=True,
                          help='Closure equation to use for the OZ equation')
    # update potential subparsers
    for pars in (parser_newton, parser_gauss_newton):
        pars.add_argument('--g-cur-ext', type=str,
                          required=True,
                          metavar='RDF_CUR_EXT',
                          help='extension of current RDF files')
        pars.add_argument('--g-cur-intra-ext', type=str,
                          metavar='RDF_CUR_INTRA_EXT',
                          help='extension of current intramol. RDF files')
        pars.add_argument('--tgt-dcdh', type=argparse.FileType('r'),
                          nargs='+', default=None,
                          help=(".npz file with dc/dh from target distributions. "
                                "If provided, will be used. "
                                "Otherwise the jacobian will be calculated from "
                                "current distributions. Multiple for multistate."))
        pars.add_argument('--flatten-at-cut-off',
                          action='store_const', const=True, default=False,
                          help=("Weather to set the last point of dU to zero. "
                                "The pressure constraint adapts to one point less."))
    # GN only options
    parser_gauss_newton.add_argument('--pressure-constraint', type=str, default=None,
                                     dest='pressure_constraint', nargs='*',
                                     help=('String of form ",p_tgt,p_cur". Starting '
                                           "comma is needed to prevent confusion when "
                                           "p_tgt is negative"))
    parser_gauss_newton.add_argument('--kirkwood-buff-constraint',
                                     action='store_const', const=True,
                                     default=False,
                                     help="Whether to match the KBI of the target RDF")
    parser_gauss_newton.add_argument('--residual-weighting',
                                     dest='residual_weighting',
                                     type=str, required=True)
    parser_gauss_newton.add_argument(
        '--tgt-dists', type=str, required=True, metavar='TGT_DISTS',
        help=('Which distributions are targeted. Pairs of names and '
              'bool. Pair separated by comma. Pairs separated by colon. '
              'true for target, false for not target.'))
    parser_gauss_newton.add_argument(
        '--upd-pots', type=str, required=True, metavar='UPD_POTS',
        help=('Which potentials are to be modified. Pairs of names and '
              'numbers. Pair separated by comma. Pairs separated by colon. '
              '1 for update, 0 for no update.'))
    # potential guess only options
    parser_pot_guess.add_argument('--subtract-coulomb',
                                  dest='subtract_coulomb',
                                  help="remove Coulomb term from potential guess",
                                  action='store_const', const=True, default=False)
    # only an argument for the potential guess, because it is evaluated per state
    parser_pot_guess.add_argument('--kBT', type=float, required=True, metavar='kBT',
                                  dest='kBT', help='Temperature times k_B')
    # GN and dcdh subparsers
    for pars in (parser_gauss_newton, parser_dcdh):
        pars.add_argument('--multistate', dest='multistate',
                          action='store_const', const=True, default=False,
                          help="enable multistate method")
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
    # multistate settings
    multistate = False
    if args.subcommand in ('gauss-newton', 'dcdh'):
        multistate = args.multistate
    if multistate:
        state_names = options.find(
            "./inverse/multistate/state_names").text.split()
    # get topology, density_dict, and n_intra_dict
    if multistate:
        topology = [ET.fromstring(top_file.read()) for top_file in args.topol]
        density_dict = [get_density_dict(top, vol) for top, vol
                        in zip(topology, args.volume)]
        n_intra_dict = [get_n_intra_dict(top) for top in topology]  # prob. indep.
    else:
        topology = ET.fromstring(args.topol[0].read())
        density_dict = get_density_dict(topology, args.volume[0])
        n_intra_dict = get_n_intra_dict(topology)
    # get charge_dict
    if args.subcommand == 'potential_guess':
        charge_dict = get_charge_dict(topology)
    # get needed intramolecular interactions
    # all topologies should contain all molecules
    intra_needed = get_intra_needed(topology[0] if multistate else topology)
    # get non_bonded_dict
    non_bonded_dict = {nb_name: nb_ts for nb_name, nb_ts in get_non_bonded(options)}
    non_bonded_dict_inv = {v: k for k, v in non_bonded_dict.items()}
    if len(non_bonded_dict) != len(non_bonded_dict_inv):
        raise Exception("Some non-bonded name was not unique or some non-bonded "
                        "interactions had the same bead types.")
    # dict of table extensions
    table_infos = {
        'g_tgt': {'extension': args.g_tgt_ext, 'check-grid': True, 'assume-zero': []}
    }
    # if potential guess or update and tgt_jacobian we need the target intramolecular
    # RDFs
    if args.subcommand in ('potential_guess', 'dcdh'):
        table_infos = {
            **table_infos,
            'G_minus_g_tgt': {
                'extension': args.g_tgt_intra_ext, 'check-grid': True,
                'assume-zero': set([
                    non_bonded_dict_inv[beadset] for beadset in (
                        set(non_bonded_dict.values()) - intra_needed
                    )
                ]),
            },
        }
    # if update, we need the current RDFs
    if args.subcommand in ('newton', 'gauss-newton'):
        table_infos = {
            **table_infos,
            'g_cur': {'extension': args.g_cur_ext, 'check-grid': True,
                      'assume-zero': []},
        }
        # if not target jacobian we need the current intramolecular RDFs
        if args.tgt_dcdh is None:
            table_infos = {
                **table_infos,
                'G_minus_g_cur': {
                    'extension': args.g_cur_intra_ext, 'check-grid': True,
                    'assume-zero': set([
                        non_bonded_dict_inv[beadset] for beadset in (
                            set(non_bonded_dict.values()) - intra_needed
                        )
                    ]),
                },
            }
    # load input arrays
    input_arrays = {}  # will hold all input data
    # Structure: [table_name, non_bonded_name, xyflag]
    # Multistate structure: [state, table_name, non_bonded_name, xyflag]
    if multistate:
        state_names_temp = state_names
    else:
        state_names_temp = ['.']  # read from current dir, dict will be collapsed later
    r_temp = None
    for state in state_names_temp:
        input_arrays[state] = {}
        for table_name, table_info in table_infos.items():
            input_arrays[state][table_name] = {}
            for non_bonded_name in non_bonded_dict.keys():
                if table_info['extension'] is None:
                    raise Exception(f"No file extension for {table_name} provided!")
                # if some interactions should not be read
                if non_bonded_name in table_info['assume-zero']:
                    assert r_temp is not None  # should never happen
                    # set all to zero
                    input_arrays[state][table_name][non_bonded_name] = {
                        'x': r_temp, 'y': np.zeros_like(r_temp),
                        'flag': np.array(['i']*len(r_temp)),
                    }
                # all others: read from file
                else:
                    x, y, flag = readin_table(f"{state}/{non_bonded_name}."
                                              f"{table_info['extension']}")
                    r_temp = x
                    input_arrays[state][table_name][non_bonded_name] = {'x': x, 'y': y,
                                                                        'flag': flag}
    # check for same grid and define r
    r = None
    for state in state_names_temp:
        for table_name, table_info in table_infos.items():
            for non_bonded_name in non_bonded_dict.keys():
                x = input_arrays[state][table_name][non_bonded_name]['x']
                if table_info['check-grid']:
                    if r is None:
                        # set first r
                        r = x
                    else:
                        # compare with first r
                        if not np.allclose(x, r):
                            raise RuntimeError("Grids of tables do not match")
    # check if starts at r = 0.0, if so: remove
    all_first_x = np.array([input_arrays[state][table_name][non_bonded_name]['x'][0]
                            for state in state_names_temp
                            for table_name, table_info in table_infos.items()
                            for non_bonded_name in non_bonded_dict.keys()])
    # if all r[0] = 0
    if np.allclose(all_first_x, np.zeros_like(all_first_x)):
        for state in state_names_temp:
            for table_name, table_info in table_infos.items():
                for non_bonded_name in non_bonded_dict.keys():
                    for key in ('x', 'y', 'flag'):
                        input_arrays[state][table_name][non_bonded_name][key] = (
                            input_arrays[state][table_name][non_bonded_name][key][1:])
        r = r[1:]
        r0_removed = True
    # if they do not start with 0 but are all the same value
    elif np.allclose(all_first_x, all_first_x[0]):
        r0_removed = False
    else:
        raise Exception('either all or no input tables should start at r=0')
    # input_arrays structure has one level less if not multistate
    if not multistate:
        assert len(input_arrays) == 1
        input_arrays = input_arrays['.']  # collapse
    del state_names_temp
    # quick access to r
    input_arrays['r'] = r
    # process input further
    if multistate:
        rhos = [gen_beadtype_property_array(dd, non_bonded_dict) for dd in density_dict]
        n_intra = [gen_beadtype_property_array(nd, non_bonded_dict) for nd
                   in n_intra_dict]
    else:
        rhos = gen_beadtype_property_array(density_dict, non_bonded_dict)
        n_intra = gen_beadtype_property_array(n_intra_dict, non_bonded_dict)
    # settings
    # copy some settings directly from args
    args_to_copy = ('closure', 'verbose', 'out', 'subcommand', 'residual_weighting',
                    'subtract_coulomb', 'kBT', 'flatten_at_cut_off')
    settings = {key: vars(args)[key] for key in args_to_copy if key in vars(args)}
    settings['non-bonded-dict'] = non_bonded_dict
    settings['rhos'] = rhos
    settings['n_intra'] = n_intra
    settings['r0-removed'] = r0_removed
    if (not multistate) and args.subcommand in ('gauss-newton', 'newton'):
        settings['kBT'] = float(options.find("./inverse/kBT").text)
    # determine dc/dh buffer
    if args.subcommand in ('newton', 'gauss-newton'):
        if args.tgt_dcdh is None:
            settings['tgt_dcdh'] = None
        else:
            # reuse dc/dh
            # close file(s), because we use np.load on file name
            map(lambda x: x.close, args.tgt_dcdh)
            try:
                settings['tgt_dcdh'] = np.load(args.tgt_dcdh[0].name)['dcdh']
            except (FileNotFoundError, ValueError):
                raise Exception("Can not load tgt_dcdh file(s) that were provided")
    # determine cut-off xml path
    if args.subcommand == 'potential_guess':
        cut_off_path = "./inverse/initial_guess/cut_off"
    elif args.subcommand in ('newton', 'gauss-newton', 'dcdh'):
        cut_off_path = "./inverse/iie/cut_off"
    else:
        raise Exception('all iie methods need a cut-off')
    # try to read it from xml ET
    try:
        settings['cut_off_pot'] = float(options.find(cut_off_path).text)
    except (AttributeError, ValueError):
        raise Exception(cut_off_path + " must be a float in settings.xml for integral "
                        "equation methods")
    # determine cut-off for residuals
    if args.subcommand in ('gauss-newton', 'dcdh'):
        cut_residual = options.find("./inverse/iie/cut_residual")
        # only if defined, not required for dc/dh, will then just use cut_off
        if cut_residual is not None:
            settings['cut_off_res'] = float(cut_residual.text)
        else:  # None if not given
            settings['cut_off_res'] = None
    else:  # None for Newton and potential_guess
        settings['cut_off_res'] = None
    # constraints
    if args.subcommand == 'gauss-newton':
        constraints = []
        if args.pressure_constraint is not None:
            if multistate:
                # Parsing is implemented here, but not tested and algo not implemented
                p_target = [float(pc.lstrip(',').split(',')[0])
                            for pc in args.pressure_constraint]
                p_current = [float(pc.lstrip(',').split(',')[1])
                             for pc in args.pressure_constraint]
                constraints.append({'type': 'pressure', 'target': p_target,
                                    'current': p_current})
            else:
                p_target = float(args.pressure_constraint[0].lstrip(',').split(',')[0])
                p_current = float(args.pressure_constraint[0].lstrip(',').split(',')[1])
                constraints.append({'type': 'pressure', 'target': p_target,
                                    'current': p_current})

        if args.kirkwood_buff_constraint:
            constraints.append({'type': 'kirkwood-buff-integral'})
        settings['constraints'] = constraints
    # stuff for subtracting coulomb potential
    if args.subcommand == 'potential_guess':
        settings['charge_dict'] = charge_dict
        settings['non_bonded_dict'] = non_bonded_dict
    # other multistate settings
    settings['multistate'] = multistate
    if multistate:
        settings['state_names'] = state_names
        settings['state_weights'] = list(map(float, options.find(
            "./inverse/multistate/state_weights").text.split()))
        settings['state_kBTs'] = list(map(float, options.find(
            "./inverse/multistate/state_kBTs").text.split()))
    # which distributions to target and which potentials to update
    if args.subcommand == 'gauss-newton':
        settings['tgt_dists'] = {pair[0]: pair[1].lower().strip() == 'true' for pair
                                 in [pair.split(',') for pair
                                     in args.tgt_dists.strip(':').split(':')]}
        settings['upd_pots'] = {pair[0]: pair[1].strip() == "1" for pair
                                in [pair.split(',') for pair
                                    in args.upd_pots.strip(':').split(':')]}
        # check that there is a value for each non-bonded interaction
        assert set(settings['tgt_dists'].keys()) == set(non_bonded_dict.keys())
        assert set(settings['upd_pots'].keys()) == set(non_bonded_dict.keys())
        # check that not all tgt_dist are False
        if not any(settings['tgt_dists'].values()):
            raise ValueError("No distribution set to be target.")

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
    # subtract Coulomb, extrapolate, and save potentials
    output_arrays = {}
    for non_bonded_name, U_dict in gen_interaction_dict(
            r, U_mat, settings['non-bonded-dict']).items():
        U = U_dict['y']
        U_flag = gen_flag_isfinite(U)
        # subtract Coulomb
        if settings['subtract_coulomb']:
            beads = tuple(settings['non_bonded_dict'][non_bonded_name])
            bead1, bead2 = (beads[0], beads[0]) if len(beads) == 1 else beads
            q1 = settings['charge_dict'][bead1]
            q2 = settings['charge_dict'][bead2]
            U_Coulomb = F_COULOMB * q1 * q2 / r
            U -= U_Coulomb
        # make tail zero. It is spoiled on the last half from inverting OZ.
        # careful: slices refer to arrays before reinserting r=0 values!
        cut, tail = calc_slices(r, settings['cut_off_pot'], verbose=False)
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
    if settings['out'] == 'none':
        return None
    for non_bonded_name, output_dict in output_arrays.items():
        fn = non_bonded_name + '.' + settings['out']
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
    cut, tail = calc_slices(r, settings['cut_off_pot'], verbose=False,
                            offset=-1)
    # generate matrices
    g_tgt_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                       settings['non-bonded-dict'])
    g_cur_mat = gen_interaction_matrix(r, input_arrays['g_cur'],
                                       settings['non-bonded-dict'])
    # calculate the ready-to-use jacobian inverse
    jac_mat, jac_inv_mat = calc_jacobian(input_arrays, settings, verbose)
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
        # add 0 in front if it was removed
        if settings['r0-removed']:
            r_out = np.concatenate(([0.0], r))
            dU = np.concatenate(([np.nan], dU))
            dU_flag = np.concatenate((['o'], dU_flag))
        else:
            r_out = r
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
    if settings['cut_off_res'] is None:
        cut, _ = calc_slices(r, settings['cut_off_pot'], verbose=False)
    else:
        cut, _ = calc_slices(r, settings['cut_off_res'], verbose=False)
    n_c = len(r[cut])
    # generate matrices
    g_tgt_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                       settings['non-bonded-dict'])
    g_cur_mat = gen_interaction_matrix(r, input_arrays['g_cur'],
                                       settings['non-bonded-dict'])
    # which distributions to use for dc/dh
    # using cur is the original Newton-Raphson root finding method
    # using tgt is a method similar to Newton's but with slope calculated at the root
    # the latter is taken from the input, is calculated at step_000 once
    if settings['tgt_dcdh'] is not None:
        dcdh = settings['tgt_dcdh']
        # the input dc/dh should already be cut to the cut-off
        assert n_c == dcdh.shape[0]
    else:
        if n_c * 2 > n_r:
            print("WARNING: max is smaller than twice of cut_off. This will lead to "
                  "artifacts in the Jacobian.")
        # generate dc/dh, invert, cut it, and invert again
        G_minus_g_cur_mat = gen_interaction_matrix(r, input_arrays['G_minus_g_cur'],
                                                   settings['non-bonded-dict'])
        # calculate dc/dh on long range
        dcdh_long = calc_dcdh(r, g_cur_mat, G_minus_g_cur_mat,
                              settings['rhos'], settings['n_intra'],
                              verbose)
        dcdh_long_2D = make_matrix_2D(dcdh_long)
        # cut invert dc/dh, cut dh/dc, invert again
        dcdh_2D = cut_matrix_inverse(dcdh_long_2D, n_r, n_i, cut)
        # make it a 4D array again
        dcdh = make_matrix_4D(dcdh_2D, n_c, n_c, n_i, n_i)
    # add the 1/g term to dc/dh and obtain inverse Jacobian
    jac_inv_mat = add_jac_inv_diagonal(r[cut], g_tgt_mat[cut], g_cur_mat[cut],
                                       dcdh, settings['rhos'],
                                       settings['n_intra'], settings['kBT'],
                                       settings['closure'], verbose)
    jac_mat = make_matrix_4D(np.linalg.inv(make_matrix_2D(jac_inv_mat)), n_c, n_c, n_i,
                             n_i)
    return jac_mat, jac_inv_mat


@if_verbose_dump_io
def calc_multistate_jacobian(input_arrays, settings, verbose=False):
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
    n_t = len(settings['rhos'][0])
    # number of interactions
    n_i = int(n_t**2)
    # number of grid points in r
    n_r = len(r)
    state_names = settings['state_names']  # shortcut
    # n_s = len(state_names)
    # slices
    if settings['cut_off_res'] is None:
        cut, _ = calc_slices(r, settings['cut_off_pot'], verbose=False)
    else:
        cut, _ = calc_slices(r, settings['cut_off_res'], verbose=False)
    n_c = len(r[cut])
    jac_mat_list = []
    if settings['tgt_dcdh'] is not None:
        dcdh_allstates = settings['tgt_dcdh']
        # the input dc/dh should already be cut to the cut-off
        assert n_c == dcdh_allstates.shape[0]
    else:
        if n_c * 2 > n_r:
            print("WARNING: max is smaller than twice of cut_off. This will lead to "
                  "artifacts in the Jacobian.")
    for s, state in enumerate(state_names):
        g_tgt_mat = gen_interaction_matrix(
            r, input_arrays[state]['g_tgt'], settings['non-bonded-dict'])
        g_cur_mat = gen_interaction_matrix(
            r, input_arrays[state]['g_cur'], settings['non-bonded-dict'])
        if settings['tgt_dcdh'] is not None:
            dcdh = dcdh_allstates[:, :, :, s*n_i:(s+1)*n_i]
        else:
            # generate dc/dh, invert, cut it, and invert again
            G_minus_g_cur_mat = gen_interaction_matrix(
                r, input_arrays[state]['G_minus_g_cur'], settings['non-bonded-dict'])
            # calculate dc/dh on long range
            dcdh_long = calc_dcdh(
                r, g_cur_mat, G_minus_g_cur_mat, settings['rhos'][s],
                settings['n_intra'][s], verbose)
            dcdh_long_2D = make_matrix_2D(dcdh_long)
            # cut invert dc/dh, cut dh/dc, invert again
            dcdh_2D = cut_matrix_inverse(dcdh_long_2D, n_r, n_i, cut)
            # make it a 4D array again
            dcdh = make_matrix_4D(dcdh_2D, n_c, n_c, n_i, n_i)
        # add the 1/g term to dc/dh and obtain inverse Jacobian
        jac_inv_mat_state = add_jac_inv_diagonal(
            r[cut], g_tgt_mat[cut], g_cur_mat[cut], dcdh,
            settings['rhos'][s], settings['n_intra'][s],
            settings['state_kBTs'][s], settings['closure'], verbose)
        jac_mat_state = make_matrix_4D(np.linalg.inv(make_matrix_2D(jac_inv_mat_state)),
                                       n_c, n_c, n_i, n_i)
        jac_mat_list.append(jac_mat_state)
    jac_mat = np.concatenate(jac_mat_list, axis=2)  # along first interaction axis
    return jac_mat


@if_verbose_dump_io
def calc_dcdh(r, g_mat, G_minus_g_mat, rhos, n_intra, verbose=False):
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
    omega, G_minus_g_hat_mat = fourier_all(r, G_minus_g_mat)
    # Fourier matrix
    F = gen_fourier_matrix(r, fourier)
    F_inv = gen_fourier_matrix(omega, fourier)
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
    n_t = len(settings['rhos'][0 if settings['multistate'] else slice(None)])
    # number of interactions
    n_i = int(n_t**2)
    # number of grid points in r
    n_r = len(r)
    # slices
    if settings['cut_off_res'] is None:
        cut, _ = calc_slices(r, settings['cut_off_pot'], verbose=False)
    else:
        cut, _ = calc_slices(r, settings['cut_off_res'], verbose=False)
    n_c = len(r[cut])
    # warning if short input data
    if n_c * 2 > n_r:
        print("WARNING: max is smaller than twice of cut_off. This will lead to "
              "artifacts in dc/dh.")
    if settings['multistate']:
        dcdh_list = []
        for s, state in enumerate(settings['state_names']):
            # generate matrices
            g_tgt_mat = gen_interaction_matrix(r, input_arrays[state]['g_tgt'],
                                               settings['non-bonded-dict'])
            # generate dc/dh, invert, cut it, and invert again
            G_minus_g_tgt_mat = gen_interaction_matrix(
                r, input_arrays[state]['G_minus_g_tgt'], settings['non-bonded-dict'])
            # calculate dc/dh on long range
            dcdh_long = calc_dcdh(
                r, g_tgt_mat, G_minus_g_tgt_mat, settings['rhos'][s],
                settings['n_intra'][s], verbose)
            dcdh_long_2D = make_matrix_2D(dcdh_long)
            # cut invert dc/dh, cut dh/dc, invert again
            dcdh_2D = cut_matrix_inverse(dcdh_long_2D, n_r, n_i, cut)
            # make it a 4D array again
            dcdh_list.append(make_matrix_4D(dcdh_2D, n_c, n_c, n_i, n_i))
        # save to npz file
        if settings['out'] != 'none':
            np.savez_compressed(settings['out'], dcdh=np.concatenate(dcdh_list, 3))
    else:
        # generate matrices
        g_tgt_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                           settings['non-bonded-dict'])
        # generate dc/dh, invert, cut it, and invert again
        G_minus_g_tgt_mat = gen_interaction_matrix(r, input_arrays['G_minus_g_tgt'],
                                                   settings['non-bonded-dict'])
        # calculate dc/dh on long range
        dcdh_long = calc_dcdh(r, g_tgt_mat, G_minus_g_tgt_mat,
                              settings['rhos'], settings['n_intra'], verbose)
        dcdh_long_2D = make_matrix_2D(dcdh_long)
        # cut invert dc/dh, cut dh/dc, invert again
        dcdh_2D = cut_matrix_inverse(dcdh_long_2D, n_r, n_i, cut)
        # make it a 4D array again
        dcdh = make_matrix_4D(dcdh_2D, n_c, n_c, n_i, n_i)
        # save to npz file
        if settings['out'] != 'none':
            np.savez_compressed(settings['out'], dcdh=dcdh)


@if_verbose_dump_io
def add_jac_inv_diagonal(r, g_tgt_mat, g_cur_mat, dcdh, rhos, n_intra, kBT,
                         closure, tgt_dcdh=None, verbose=False):
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
    jac_inv_mat = np.zeros((len(r), len(r), n_i, n_i))
    if closure == 'hnc':
        with np.errstate(divide='ignore', invalid='ignore', under='ignore'):
            for h, (i, j) in enumerate(itertools.product(range(n_i), range(n_i))):
                if i == j:
                    diagonal_term = np.diag(1 - 1 / g_avg_vec[:, i])
                else:
                    diagonal_term = 0
                jac_inv_mat[:, :, i, j] = kBT * (diagonal_term - dcdh[:, :, i, j])
    elif closure == 'py':
        # implementation is a bit tricky because one needs c_mat again, but
        # if tgt_dcdh the intramolecular interactions are not present
        raise NotImplementedError
    # make negative infinities a large negative number, increasing stability
    jac_inv_mat[np.isneginf(jac_inv_mat)] = -1e30
    return jac_inv_mat


def gen_indices_upd_pots_tgt_dists(settings):
    index_tgt_dists = []
    index_not_tgt_dists = []
    index_upd_pots = []
    index_not_upd_pots = []
    bead_types = get_bead_types(settings['non-bonded-dict'])
    non_bonded_dict_inv = {v: k for k, v in settings['non-bonded-dict'].items()}
    for i, ((b1, bead1), (b2, bead2)) in enumerate(
            (((b1, bead1), (b2, bead2))
             for b1, bead1 in enumerate(bead_types)
             for b2, bead2 in enumerate(bead_types)
             )):
        interaction_name = non_bonded_dict_inv[frozenset({bead1, bead2})]
        if settings['tgt_dists'][interaction_name] is True:
            index_tgt_dists.append(i)
        else:
            index_not_tgt_dists.append(i)
        if settings['upd_pots'][interaction_name] is True:
            index_upd_pots.append(i)
        else:
            index_not_upd_pots.append(i)
    return (
        index_tgt_dists, index_not_tgt_dists,
        index_upd_pots, index_not_upd_pots,
    )


def gen_dfdu_matrix(n_c_pot, n_c_res, Delta_r):
    """Generate with df/du partial derivatives.

    There are as many grid points for forces as there are for the potential (w/o
    cut-off). The grid of the forces is shifted relative to the potential/RDF grid:
    r + Δr/2.
    """
    dfdu = np.zeros((n_c_pot, n_c_res))
    # create indices for changing the main- and super-diagonal
    main_diag = np.diag_indices(n_c_pot, ndim=2)
    # copy needed, because diag_indices generates twice the same object
    super_diag = list(np.diag_indices(n_c_pot-1, ndim=2)[i].copy() for i in (0, 1))
    # shift to geth the super diagonal
    super_diag[1] += 1
    super_diag = tuple(super_diag)  # tuple needed for indexing
    dfdu[main_diag] = -1 / Delta_r
    dfdu[super_diag] = +1 / Delta_r
    return dfdu


@if_verbose_dump_io
def gauss_newton_update(input_arrays, settings, verbose=False):
    """Calculate Gauss-Newton potential update based on s.a. RISM-OZ and closure.

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
    # grid spacing
    Delta_r = calc_grid_spacing(r)
    # slices and indices
    # we don't want to update at cut-off, therefore offset=-1
    cut_pot, tail_pot = calc_slices(r, settings['cut_off_pot'], verbose=False,
                                    offset=-1)
    # sometimes we need to refer to values including the value of the cut-off
    cut_pot_p1, tail_pot_p1 = calc_slices(r, settings['cut_off_pot'], verbose=False,
                                          offset=0)
    cut_res, tail_res = calc_slices(r, settings['cut_off_res'], verbose=False)
    n_c_pot = len(r[cut_pot])
    n_c_res = len(r[cut_res])
    # generate matrices from dicts
    g_tgt_mat = gen_interaction_matrix(r, input_arrays['g_tgt'],
                                       settings['non-bonded-dict'])
    g_cur_mat = gen_interaction_matrix(r, input_arrays['g_cur'],
                                       settings['non-bonded-dict'])
    # calculate the ready-to-use jacobian inverse
    jac_mat, _ = calc_jacobian(input_arrays, settings, verbose)
    # weighting
    g_tgt_vec = vectorize(g_tgt_mat)
    weights = gen_residual_weights(settings['residual_weighting'], r[cut_res], n_c_res,
                                   n_i, 1, g_tgt_vec[cut_res])
    # Delta g for potential update
    Delta_g_mat = g_cur_mat - g_tgt_mat
    # vectorize Delta g
    Delta_g_vec = vectorize(Delta_g_mat)
    # one might not want to take all dists in account or update all potentials
    # determine which rows/cols should be removed
    (index_tgt_dists, index_not_tgt_dists,
     index_upd_pots, index_not_upd_pots) = gen_indices_upd_pots_tgt_dists(settings)
    n_tgt_dists = len(index_tgt_dists)
    n_upd_pots = len(index_upd_pots)
    # remove rows and columns from jacobian
    jac_mat = np.delete(jac_mat, index_not_tgt_dists, axis=-2)  # del rows
    jac_mat = np.delete(jac_mat, index_not_upd_pots, axis=-1)  # del cols
    # remove cols from weights
    weights = np.delete(weights, index_not_tgt_dists, axis=1)  # del cols
    # remove cols from Δg vector
    Delta_g_vec = np.delete(Delta_g_vec, index_not_tgt_dists, axis=1)  # del cols
    # remove rows and columns from target RDF matrix for pressure constraint
    g_tgt_vec = vectorize(g_tgt_mat)
    g_tgt_vec = np.delete(g_tgt_vec, index_not_upd_pots, axis=-2)  # del rows
    g_tgt_vec = np.delete(g_tgt_vec, index_not_upd_pots, axis=-1)  # del cols
    # make Jacobian 2D
    jac_2D = make_matrix_2D(jac_mat)
    # prepare A0 and C matrix
    if 'pressure-old' in (c['type'] for c in settings['constraints']):
        # update force rather than potential
        # antiderivative operator (upper triangular matrix on top of zeros)
        A0_2D = kron_2D(np.identity(n_upd_pots),
                        Delta_r * np.triu(np.ones((n_c_res, n_c_pot-1)), k=0))
        C = np.zeros((len(settings['constraints']), n_upd_pots * (n_c_pot-1)))
    else:
        # update potential, A0 just cuts of the tail
        A0_2D = kron_2D(np.identity(n_upd_pots),
                        np.append(np.identity(n_c_pot),
                                  np.zeros((n_c_res-n_c_pot, n_c_pot)), axis=0))
        C = np.zeros((len(settings['constraints']), n_upd_pots * n_c_pot))
    # weight Jacobian
    jac_2D_weighted = np.diag(weights.T.flatten()) @ jac_2D
    # cut and weight Delta_g and obtain residuals
    residuals_vec = np.zeros((n_c_res, n_tgt_dists))
    for i in range(n_tgt_dists):
        residuals_vec[:, i] = np.diag(weights[:, i]) @ Delta_g_vec[cut_res, i]
    # flatten residuals
    residuals_flat = residuals_vec.T.flatten()
    # we will solve the least squares problem |A @ x - b| with constraints C @ x - d
    A = jac_2D_weighted @ A0_2D
    b = residuals_flat
    d = np.zeros(len(settings['constraints']))
    # prepare all constraints
    for c, constraint in enumerate(settings['constraints']):
        if constraint['type'] == 'pressure-old':
            # current pressure
            p = constraint['current'] / BAR_PER_MD_PRESSURE
            # target pressure
            p_tgt = constraint['target'] / BAR_PER_MD_PRESSURE
            # print if verbose
            if settings['verbose']:
                print(f"Constraining pressure: target is {constraint['target']} bar, "
                      f"current value is {constraint['current']} bar")
            # g_tgt(r_{i+1})
            g_tgt_ip1 = g_tgt_vec[cut_pot][1:].T.flatten()
            # g_tgt(r_{i})
            g_tgt_i = g_tgt_vec[cut_pot][:-1].T.flatten()
            # r_{i}
            r_i = np.tile(r[cut_pot][:-1], n_upd_pots)
            # r_{i+1}
            r_ip1 = np.tile(r[cut_pot][1:], n_upd_pots)
            # density product ρ_i * ρ_j as vector of same length as r_i
            rho_i = np.repeat(
                vectorize(
                    np.outer(*([settings['rhos']]*2))
                )[index_upd_pots], n_c_pot-1
            )
            # l vector
            ll = (g_tgt_i + g_tgt_ip1) * (r_ip1**4 - r_i**4)
            ll *= -1/12 * np.pi * rho_i
            # ll has differt sign than in Delbary, since different definition of b
            # I think this does not need another factor for mixed interactions
            # internally we have u_ij and u_ji seperate, but so we have in the virial
            # expression for mixtures
            # set C row and d element
            if settings['flatten_at_cut_off']:
                ll[n_c_pot-2::n_c_pot-1] = 0  # no constraint for last point of each dU
            C[c, :] = ll
            d[c] = p_tgt - p
        elif constraint['type'] == 'pressure':
            if n_t > 1:
                raise NotImplementedError("pressure constraint not implemented for "
                                          "more than one bead")
            # current pressure
            p = constraint['current'] / BAR_PER_MD_PRESSURE
            # target pressure
            p_tgt = constraint['target'] / BAR_PER_MD_PRESSURE
            # print if verbose
            if settings['verbose']:
                print(f"Constraining pressure: target is {constraint['target']} bar, "
                      f"current value is {constraint['current']} bar")
            # define df/du matrix
            # use short range for both f and u
            dfdu = gen_dfdu_matrix(n_c_pot, n_c_pot, Delta_r)
            # volume of sphere shell element
            r3_dr = ((r[cut_pot] + Delta_r)**4 - r[cut_pot]**4) / 4
            # average RDF in the shell
            g_tgt_avg = (g_tgt_vec[cut_pot_p1][:-1].T.flatten()
                         + g_tgt_vec[cut_pot_p1][1:].T.flatten()) / 2
            dpdf = -2/3 * np.pi * settings['rhos'][0]**2 * g_tgt_avg * r3_dr
            C[c, :] = dpdf @ dfdu
            if settings['flatten_at_cut_off']:
                C[c, n_c_pot-1::n_c_pot] = 0  # no constraint for last point of each dU
            d[c] = p - p_tgt
        elif constraint['type'] == 'kirkwood-buff-integral':
            if n_t > 1:
                raise NotImplementedError("KBI constraint not implemented for more "
                                          "than one bead")
            # we leave out pre factors (rho should come back for multiple beads)
            # current KBI
            G = 4 * np.pi * np.sum(r[cut_res]**2
                                   * (g_cur_mat[cut_res, 0, 0] - 1)) * Delta_r
            # target KBI
            G_tgt = 4 * np.pi * np.sum(r[cut_res]**2
                                       * (g_tgt_mat[cut_res, 0, 0] - 1)) * Delta_r
            # print if verbose
            if settings['verbose']:
                print(f"Constraining Kirkwood-Buff integral from 0 to "
                      f"r={r[cut_res][-1]:.4f} nm: target is {G_tgt:.5} nm³, current "
                      f"value is {G:.5} nm³")
            # define C row
            C[c, :] = 4 * np.pi * Delta_r * r[cut_res]**2 @ jac_2D @ A0_2D
            d[c] = G - G_tgt
        # elif constraint['type'] == 'potential_energy_relation':
            # idea is to have a relation between integral g_i u_i dr and
            # integral g_j u_j dr, for example to prohibit phase separation
        else:
            raise NotImplementedError("not implemented constraint type: "
                                      + constraint['type'])
    # solve least squares problem with linear constraints
    dx_flat = solve_lsq_with_linear_constraints(A, C, b, d)
    # obtain potential update (dx_flat can be potential or force)
    dU_flat = -A0_2D @ dx_flat
    # test KBI
    Delta_g_predicted = jac_2D @ dU_flat
    if settings['verbose']:
        np.savez_compressed('Delta_g_predicted.npz',
                            Delta_g_predicted=Delta_g_predicted)
    # end test KBI
    dU_vec = dU_flat.reshape(n_upd_pots, n_c_res).T
    # insert zeros for ignored potentials
    for i in index_not_upd_pots:
        dU_vec = np.insert(dU_vec, i, np.zeros(n_c_res), axis=1)
    # dU matrix
    dU_mat = devectorize(dU_vec)
    # cut off tail
    # dU_mat = dU_mat[cut_pot]
    # prepare output
    output_arrays = {}
    for non_bonded_name, dU_dict in gen_interaction_dict(
            r[cut_res], dU_mat, settings['non-bonded-dict']).items():
        r_out = dU_dict['x']
        dU = dU_dict['y']
        dU_flag = gen_flag_isfinite(dU)
        dU_flag[tail_pot_p1] = 'o'
        # shift potential to make value before cut-off zero
        if settings['flatten_at_cut_off']:
            dU[cut_pot] -= dU[cut_pot][-1]
        # add 0 in front if it was removed
        if settings['r0-removed']:
            r_out = np.concatenate(([0.0], r_out))
            dU = np.concatenate(([np.nan], dU))
            dU_flag = np.concatenate((['o'], dU_flag))
        # change NaN in the core region to first valid value
        dU = extrapolate_dU_left_constant(dU, dU_flag)
        # save for output
        output_arrays[non_bonded_name] = {'x': r_out, 'y': dU, 'flag': dU_flag}
    return output_arrays


@if_verbose_dump_io
def multistate_gauss_newton_update(input_arrays, settings, verbose=False):
    """Calculate Gauss-Newton potential update based on s.a. RISM-OZ and closure.

    Args:
        input_arrays: nested dict holding the distributions
        settings: dict holding relevant settings
        verbose: save parameters and return of this and contained functions as numpy
                 file

    Returns:
        dictionary of potential updates including flags to be saved
    """
    # ######## MULTISTATE ######### #
    # MULTISTATE ################## #
    # ################## MULTISTATE #
    # ######## MULTISTATE ######### #

    # obtain r
    r = input_arrays['r']
    # number of atom types
    n_t = len(settings['rhos'][0])
    # number of interactions including redundand ones
    n_i = int(n_t**2)
    # number of states
    state_names = settings['state_names']  # shortcut
    n_s = len(state_names)
    # grid spacing
    Delta_r = calc_grid_spacing(r)
    # slices
    # we don't want to update at cut-off, therefore offset=-1
    cut_pot, tail_pot = calc_slices(r, settings['cut_off_pot'], verbose=False,
                                    offset=-1)
    # sometimes we need to refer to values including the value of the cut-off
    cut_pot_p1, tail_pot_p1 = calc_slices(r, settings['cut_off_pot'], verbose=False,
                                          offset=0)
    cut_res, tail_res = calc_slices(r, settings['cut_off_res'], verbose=False)
    n_c_pot = len(r[cut_pot])
    n_c_res = len(r[cut_res])
    # generate matrices
    # g_tgt and g_cur, [r, state * interaction]
    g_tgt_vec = np.concatenate([vectorize(gen_interaction_matrix(
        r, input_arrays[s]['g_tgt'], settings['non-bonded-dict']))
                                for s in state_names], axis=1)
    g_cur_vec = np.concatenate([vectorize(gen_interaction_matrix(
        r, input_arrays[s]['g_cur'], settings['non-bonded-dict']))
                                for s in state_names], axis=1)
    # calculate the ready-to-use jacobian inverse
    jac_mat = calc_multistate_jacobian(input_arrays, settings, verbose)
    # make Jacobian 2D
    jac_2D = make_matrix_2D(jac_mat)
    del jac_mat  # saving memory
    # weights
    weights = gen_residual_weights(settings['residual_weighting'], r[cut_res], n_c_res,
                                   n_i, n_s, g_tgt_vec[cut_res])
    # weight Jacobian
    jac_2D = np.diag(weights.T.flatten()) @ jac_2D
    # Delta g for potential update
    Delta_g_vec = g_cur_vec - g_tgt_vec
    # cut and weight Delta_g and obtain residuals
    residuals_vec = np.zeros((n_c_res, n_s * n_i))
    for s in range(n_s):
        for i in range(n_i):
            residuals_vec[:, s*n_i+i] = (np.diag(weights[:, s*n_i+i])
                                         @ Delta_g_vec[cut_res, s*n_i+i])
    # flatten residuals
    residuals_flat = residuals_vec.T.flatten()
    # TODO: pressure constraint for multistate
    # update force rather than potential
    # antiderivative operator (upper triangular matrix on top of zeros)
    A0_2D = kron_2D(np.identity(n_i),
                    Delta_r * np.triu(np.ones((n_c_res, n_c_pot-1)), k=0))
    C = np.zeros((0, n_i * (n_c_pot-1)))
    A = jac_2D @ A0_2D
    b = residuals_flat
    d = np.zeros(0)
    # solve linear equation
    dU_flat = -A0_2D @ solve_lsq_with_linear_constraints(A, C, b, d)
    dU_vec = dU_flat.reshape(n_i, n_c_res).T
    # dU matrix
    dU_mat = devectorize(dU_vec)
    # dU_mat = dU_mat[cut_pot]
    # prepare output
    output_arrays = {}
    for non_bonded_name, dU_dict in gen_interaction_dict(
            r[cut_res], dU_mat, settings['non-bonded-dict']).items():
        r_out = dU_dict['x']
        dU = dU_dict['y']
        dU_flag = gen_flag_isfinite(dU)
        dU_flag[tail_pot_p1] = 'o'
        # shift potential to make value before cut-off zero
        if settings['flatten_at_cut_off']:
            dU[cut_pot] -= dU[cut_pot][-1]
        # add 0 in front if it was removed
        if settings['r0-removed']:
            r_out = np.concatenate(([0.0], r_out))
            dU = np.concatenate(([np.nan], dU))
            dU_flag = np.concatenate((['o'], dU_flag))
        else:
            r_out = r
        # change NaN in the core region to first valid value
        dU = extrapolate_dU_left_constant(dU, dU_flag)
        # save for output
        output_arrays[non_bonded_name] = {'x': r_out, 'y': dU, 'flag': dU_flag}
    return output_arrays


@if_verbose_dump_io
def zero_update(input_arrays, settings, verbose=False):
    """Calculate Gauss-Newton potential update based on s.a. RISM-OZ and closure.

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
    # slices
    # we don't want to update at cut-off, therefore offset=-1
    cut_pot, tail_pot = calc_slices(r, settings['cut_off_pot'], verbose=False,
                                    offset=-1)
    # sometimes we need to refer to values including the value of the cut-off
    _, tail_pot_p1 = calc_slices(r, settings['cut_off_pot'], verbose=False,
                                 offset=0)
    cut_res, tail_res = calc_slices(r, settings['cut_off_res'], verbose=False)
    n_c_res = len(r[cut_res])
    dU_mat = np.zeros((n_c_res, n_t, n_t))
    # prepare output
    output_arrays = {}
    for non_bonded_name, dU_dict in gen_interaction_dict(
            r[cut_res], dU_mat, settings['non-bonded-dict']).items():
        r_out = dU_dict['x']
        dU = dU_dict['y']
        dU_flag = gen_flag_isfinite(dU)
        dU_flag[tail_pot_p1] = 'o'
        if settings['r0-removed']:
            r_out = np.concatenate(([0.0], r_out))
            dU = np.concatenate(([0.0], dU))
            dU_flag = np.concatenate((['o'], dU_flag))
        # save for output
        output_arrays[non_bonded_name] = {'x': r_out, 'y': dU, 'flag': dU_flag}
    return output_arrays


def gen_residual_weights(res_weight_scheme, r, n_c_res, n_i, n_s, g_tgt_vec):
    if res_weight_scheme == 'unity':
        return np.ones((n_c_res, n_s * n_i))
    if '_over_' not in res_weight_scheme:
        raise Exception('Unknown weighting scheme:', res_weight_scheme)
    weights = np.zeros((n_c_res, n_s * n_i))
    if res_weight_scheme.startswith('one_plus_'):
        weights += 1
        res_weight_scheme = res_weight_scheme[len('one_plus_'):]
    numerator_key, denominator_key = res_weight_scheme.split('_over_')
    if numerator_key == 'one':
        numerator = 1
    elif numerator_key == 'r':
        numerator = np.repeat(np.transpose([r]), n_s * n_i, 1)
    elif numerator_key == 'r_square':
        numerator = np.repeat(np.transpose([r**2]), n_s * n_i, 1)
    else:
        raise Exception('Unknown weighting scheme numerator:', numerator_key)
    if denominator_key == 'one':
        denominator = 1
    elif denominator_key == 'sqrt_rdf':
        # we have to add a small number here, otherwise the inverse below fails
        # alternatively one could could cut each sub block but that would get tedious
        denominator = np.sqrt(g_tgt_vec) + 1e-30
    elif denominator_key == 'rdf':
        denominator = g_tgt_vec + 1e-30
    else:
        raise Exception('Unknown weighting scheme denominator:', denominator_key)
    weights = weights + numerator / denominator
    return weights


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


def calc_slices(r, cut_off, verbose=False, offset=0):
    """
    Generate slices for the regions used in the IIE methods.
    For Gauss-Newton this function is called twice, once for the cut_off of the
    potential and once for the cut-off of the resiudals.

    There are different regions used:
    |        cut             |       tail       |  # regions
    0---------------------cut_off-----------r[-1]  # distances
    0---------------------ndx_co---------len(r)+1  # indices
    note: in earlier versions, there were slices (nocore, crucial) that
    excluded the core region (rdv < threshold)
    """
    ndx_co = find_after_cut_off_ndx(r, cut_off) + offset
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


if __name__ == '__main__':
    main()
