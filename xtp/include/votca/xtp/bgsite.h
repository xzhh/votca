/*
 *            Copyright 2009-2020 The VOTCA Development Team
 *                       (http://www.votca.org)
 *
 *      Licensed under the Apache License, Version 2.0 (the "License")
 *
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *              http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

#pragma once
#ifndef VOTCA_XTP_EWALDSITE_H
#define VOTCA_XTP_EWALDSITE_H

// Local VOTCA includes
#include "votca/xtp/classicalsegment.h"
#include "votca/xtp/multipole.h"

#include <iomanip>

namespace votca {
namespace xtp {

class BGSite {
 public:
  BGSite(const PolarSite& pol);

  ~BGSite() = default;

  const Eigen::Vector3d& getPos() const { return position_; }

  void updatePos(Eigen::Vector3d pos) {position_ = pos;}

  const std::string& getElement() const { return element_;}

  void addToStaticField(Eigen::Vector3d field) {static_field_ += field;}

 private:
  Index id_;
  Eigen::Vector3d position_;
  Multipole mp_;
  Eigen::Vector3d induced_dipole_ = Eigen::Vector3d::Zero();
  std::string element_;
  Eigen::Matrix3d polarization_;
  Eigen::Vector3d static_field_ = Eigen::Vector3d::Zero();
  Eigen::Vector3d induced_field_ = Eigen::Vector3d::Zero();
};
}  // namespace xtp
}  // namespace votca
#endif