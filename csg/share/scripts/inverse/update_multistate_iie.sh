#! /bin/bash
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

if [ "$1" = "--help" ]; then
cat <<EOF
${0##*/}, version %version%
This script implements the potential update for the multistate iterative integral equation
methods

Usage: ${0##*/}  [--help]
EOF
   exit 0
fi

iie_method="$(csg_get_property cg.inverse.iie.method)"
sim_prog="$(csg_get_property cg.inverse.program)"
state_names="$(csg_get_property cg.inverse.multistate.state_names)"

# newton not supported any more
if [[ ($iie_method == newton-mod) || ($iie_metho == newton) ]]; then
  die "Multistate only works as a Gauss-Newton method (more residuals than potentials)"
fi

# pressure constraint
#pressure_constraint="$(csg_get_property cg.inverse.iie.pressure_constraint)"
#if is_num "${pressure_constraint}"; then
  ## TODO: check if n_s numbers and get pressure from each simulation
    #p_file="${name}.pressure"
    #do_external pressure "$sim_prog" "$p_file"
    #p_now="$(sed -n 's/^Pressure=\(.*\)/\1/p' "$p_file")" || die "${0##*/}: sed of Pressure failed"
    #[[ -z $p_now ]] && die "${0##*/}: Could not get pressure from simulation"
    #echo "New pressure $p_now, target pressure $pressure_constraint"
    #pressure_constraint_flag="--pressure-constraint $pressure_constraint,$p_now"
#else
msg "No pressure matching for multistate implemented"
pressure_constraint_flag=""
#fi

# Gauss-Newton residual weighting
residual_weighting_flag="--residual-weighting $(csg_get_property cg.inverse.iie.residual_weighting)"

# target dc/dh
if [[ $(csg_get_property cg.inverse.iie.tgt_dcdh) == true ]]; then
  tgt_dcdh_flag="--tgt-dcdh dcdh.npz"
else
  g_intra_flag="--g-cur-intra-ext dist-intra.new"
fi

# topology for molecular conections and volume
for state in $state_names; do
  topol_state="${state}/$(csg_get_property --allow-empty cg.inverse.iie.topol)"
  [[ -z $topol_state ]] && topol_state="${state}/$(csg_get_property cg.inverse.$sim_prog.topol)"
  [[ -f $topol_state ]] || die "${0##*/}: topol file '$topol_state' not found, possibly you have to add it to cg.inverse.filelist"
  volume_state=$(critical csg_dump --top "$topol_state" | grep 'Volume' | awk '{print $2}')
  ([[ -n "$volume_state" ]] && is_num "$volume_state") || die "could not determine the volume from file ${topol_state}"
  # append
  topol="$topol $topol_state"
  volume="$volume $volume_state"
done

verbose=$(csg_get_property cg.inverse.iie.verbose)

g_extrap_factor=$(csg_get_property --allow-empty cg.inverse.iie.g_extrap_factor) 
[[ -n $g_extrap_factor ]] && msg --color blue "Deprecated option g_extrap_factor will be ignored!"

[[ "${verbose}" == 'true' ]] && verbose_flag="--verbose"

# calc RDFs
# for_all not necessary for most sim_prog, but also doesn't hurt.
for state in $state_names; do
  pushd "$state"
  for_all "non-bonded bonded" do_external rdf $sim_prog
  # calculate distributions intramolecular
  if [[ $(csg_get_property cg.inverse.iie.tgt_dcdh) != 'true' ]]; then
    for_all "non-bonded" do_external rdf "$sim_prog" --only-intra-nb
  fi
  popd
done

# resample target distributions
for state in $state_names; do
  pushd "$state"
  for_all "non-bonded" do_external resample target --clean '$(csg_get_interaction_property inverse.target)' '$(csg_get_interaction_property name).dist.tgt'
  if [[ $(csg_get_property cg.inverse.iie.tgt_dcdh) != 'true' ]]; then
    for_all "non-bonded" do_external resample target --no-extrap '$(csg_get_interaction_property inverse.target_intra)' '$(csg_get_interaction_property name).dist-intra.tgt'
  fi
  popd
done

# Some arguments (cut_off, state_names, state_kBTs, ..) will be read directly from the settings.xml. They do not have a default in csg_defaults.xml.
# Others (closure, multistate.enabled, ...) could also be read from the settings file, but this bash script handles the defaults.
do_external update iie_pot "$iie_method" \
  ${verbose_flag-} \
  --multistate \
  --closure "$(csg_get_property cg.inverse.iie.closure)" \
  --volume $volume \
  --topol $topol \
  --options "$CSGXMLFILE" \
  --g-tgt-ext "dist.tgt" \
  --g-cur-ext "dist.new" \
  ${g_intra_flag-} \
  --out "dpot.new" \
  ${pressure_constraint_flag-} \
  ${residual_weighting_flag-} \
  ${tgt_dcdh_flag-}

# overwrite with zeros if do_potential=0
do_potential_zero_overwrite() {
  step_nr=$(get_current_step_nr)
  scheme=( $(csg_get_interaction_property inverse.do_potential) )
  scheme_nr=$(( ( $step_nr - 1 ) % ${#scheme[@]} ))
  name=$(csg_get_interaction_property name)
  if [[ ${scheme[$scheme_nr]} == 0 ]]; then
    echo "Update potential ${name} : no"
    min=$(csg_get_interaction_property min)
    max=$(csg_get_interaction_property max)
    step=$(csg_get_interaction_property step)
    critical rm "${name}.dpot.new"
    do_external table dummy "${min}:${step}:${max}" "${name}.dpot.new"
  fi
}
export -f do_potential_zero_overwrite
for_all "non-bonded" do_potential_zero_overwrite

for_all "bonded" do_external update ibi_single