#! /bin/bash

if [ "$1" = "--help" ]; then
   echo This script implemtents the function update
   echo for the Inverse Monte Carlo Method
   echo Usage: ${0##*/} step_nr
   echo Needs:  run_or_exit, \$source_wrapper, update_POT.pl
   exit 0
fi

[[ -n "$1" ]] || die "${0##*/}: Missing argument"


cgmap=$(get_property cgmap)
topol=$(get_sim_property gromacs.topol)
traj=$(get_sim_property gromacs.traj)

msg "calculating statistics"
run_or_exit csg_imc --options $CSGXMLFILE --top $topol --trj $traj --cg $cgmap

msg "solving linear equations for dpot"
# todo: allow to specify solver in xml
sed -e "s/%NAME/CG-CG/" $CSGSHARE/linsolve.m > imcsolve.m
/sw/linux/suse/client/matlab/bin/matlab -arch=glnx86 -r imcsolve -nosplash -nodesktop
rm -f imcsolve.m

# temporary compatibility issue

sed -ie 's/NaN/0.0/' CG-CG.dpot.new
sed -ie 's/Inf/0.0/' CG-CG.dpot.new

cp CG-CG.dpot.new CG_CG.dpot.new