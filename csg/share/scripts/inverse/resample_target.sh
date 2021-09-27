#! /bin/bash
#
# Copyright 2009-2011 The VOTCA Development Team (http://www.votca.org)
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
show_help () {
cat <<EOF
${0##*/}, version %version%
This script resamples distribution to grid spacing of the setting xml file and extrapolates if needed

Usage: ${0##*/} [options] input output

Allowed options:
    --help       show this help
    --no-extrap  do no extrapolation, e.g. for intramolecular non-bonded
EOF
}

do_extrap="yes"

### begin parsing options
shopt -s extglob
while [[ ${1} = --* ]]; do
  case $1 in
  --no-extrap)
    do_extrap="no"
    shift ;;
  -h | --help)
    show_help
    exit 0;;
  *)
    die "Unknown option '$1'";;
  esac
done
[[ -z $1 || -z $2 ]] && die "${0##*/}: Missing arguments"

[[ -z $1 || -z $2 ]] && die "${0##*/}: Missing argument"
input="$1"
main_dir=$(get_main_dir)
[[ -f ${main_dir}/$input ]] || die "${0##*/}: Could not find input file '$input' in maindir ($main_dir)"
output="$2"

min=$(csg_get_interaction_property min )
max=$(csg_get_interaction_property max )
step=$(csg_get_interaction_property step )
name=$(csg_get_interaction_property name)
tabtype="$(csg_get_interaction_property bondtype)"

comment="$(get_table_comment)"
# resample
resampled="$(critical mktemp ${name}.dist.tgt_resampled.XXXXX)"
critical csg_resample --in ${main_dir}/${input} --out ${resampled} --grid ${min}:${step}:${max} --comment "${comment}"
# extrapolate
if [[ $do_extrap == "yes" ]]; then
  extrapolated="$(critical mktemp ${name}.dist.tgt_extrapolated.XXXXX)"
  if [[ $tabtype == "non-bonded" ]]; then
    # extrapolate left
    extrapolated2="$(critical mktemp ${name}.dist.tgt_extrapolated_left.XXXXX)"
    do_external table extrapolate --function linear --avgpoints 1 --region left "${resampled}" "${extrapolated2}"
    # improve RDF in the core region where it is close to zero and sampling is bad using
    # an extrapolation scheme from the better sampled onset of the RDF
    improve_dist_near_core_target="$(csg_get_interaction_property improve_dist_near_core.target)"
    if [[ $improve_dist_near_core_target == "true" ]]; then
      improve_dist_near_core_function="$(csg_get_interaction_property improve_dist_near_core.function)"
      extrapolated3="$(critical mktemp ${name}.dist.tgt_improved.XXXXX)"
      fit_start_g="$(csg_get_interaction_property improve_dist_near_core.fit_start_g)"
      fit_end_g="$(csg_get_interaction_property improve_dist_near_core.fit_end_g)"
      do_external dist improve_near_core --in="${extrapolated2}" --out="${extrapolated3}" \
      --function="$improve_dist_near_core_function" \
      --gmin="$fit_start_g" --gmax="$fit_end_g" 
    elif [[ $improve_dist_near_core_target == "false" ]]; then
      extrapolated3="$extrapolated2"
    else
      die "${0##*/}: improve_dist_near_core.target is ${improve_dist_near_core_target}. Needs to be 'true' or 'false'"
    fi
    # extrapolate right
    do_external table extrapolate --function constant --avgpoints 1 --region right "${extrapolated3}" "${extrapolated}"
  elif [[ $tabtype == bond || $tabtype == angle || $tabtype == dihedral ]]; then
    # extrapolate on both sides
    do_external table extrapolate --function linear --avgpoints 1 --region leftright "${resampled}" "${extrapolated}"
  else
    die "${0##*/}: Resample of distribution of type $tabtype is not implemented yet"
  fi
else
  extrapolated="${resampled}"
fi
do_external dist adjust "${extrapolated}" "${output}"
