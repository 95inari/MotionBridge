using System;
using System.Collections.Generic;
using Unity.Collections;
using UnityEngine;
using UnityEngine.Animations;
using UnityEngine.Playables;

namespace Kuko.MotionBridge.Retargeting
{
    /// <summary>
    /// Owns a manual PlayableGraph that writes a complete HumanPose to one Humanoid Animator.
    /// Animator configuration and transform restoration remain the caller's responsibility.
    /// </summary>
    public sealed class HumanoidPosePlayable : IDisposable
    {
        private Animator _target;
        private PlayableGraph _graph;
        private AnimationScriptPlayable _posePlayable;
        private NativeArray<MuscleHandle> _muscleHandles;
        private NativeArray<float> _muscles;
        private bool _disposed;

        public HumanoidPosePlayable(
            Animator target,
            string graphName = "Motion Bridge Humanoid Pose")
        {
            ValidateTarget(target);
            if (string.IsNullOrWhiteSpace(graphName))
                throw new ArgumentException("Graph name is required.", nameof(graphName));

            _target = target;
            try
            {
                var managedMuscleHandles = BuildHumanPoseOrderedMuscleHandles();
                _muscleHandles = new NativeArray<MuscleHandle>(
                    managedMuscleHandles,
                    Allocator.Persistent);
                _muscles = new NativeArray<float>(
                    managedMuscleHandles.Length,
                    Allocator.Persistent,
                    NativeArrayOptions.ClearMemory);

                _graph = PlayableGraph.Create(graphName);
                _graph.SetTimeUpdateMode(DirectorUpdateMode.Manual);
                var job = new HumanPoseWriterJob
                {
                    MuscleHandles = _muscleHandles,
                    Muscles = _muscles,
                    BodyPosition = Vector3.zero,
                    BodyRotation = Quaternion.identity,
                    TrajectoryPosition = Vector3.zero,
                    TrajectoryRotation = Quaternion.identity,
                };
                _posePlayable = AnimationScriptPlayable.Create(_graph, job, 0);
                _posePlayable.SetProcessInputs(false);
                var output = AnimationPlayableOutput.Create(
                    _graph,
                    "Motion Bridge Humanoid Output",
                    target);
                output.SetSourcePlayable(_posePlayable);
                output.SetWeight(1f);
                _graph.Play();
            }
            catch
            {
                CleanupResources();
                _disposed = true;
                throw;
            }
        }

        public Animator Target => !_disposed && _target != null ? _target : null;

        public PlayableGraph Graph => _graph;

        public bool IsValid => !_disposed && _target != null && _graph.IsValid();

        public void SetPose(HumanPose pose)
        {
            SetPose(pose, Vector3.zero, Quaternion.identity);
        }

        public void SetPose(
            HumanPose pose,
            Vector3 trajectoryPosition,
            Quaternion trajectoryRotation)
        {
            EnsureValid();
            ValidatePose(pose);
            ValidateTrajectory(trajectoryPosition, trajectoryRotation);

            for (var index = 0; index < _muscles.Length; index++)
                _muscles[index] = pose.muscles[index];

            var job = _posePlayable.GetJobData<HumanPoseWriterJob>();
            job.BodyPosition = pose.bodyPosition;
            job.BodyRotation = Normalize(pose.bodyRotation);
            job.TrajectoryPosition = trajectoryPosition;
            job.TrajectoryRotation = Normalize(trajectoryRotation);
            _posePlayable.SetJobData(job);
        }

        public void Evaluate(float deltaTime = 0f)
        {
            EnsureValid();
            if (!IsFinite(deltaTime) || deltaTime < 0f)
            {
                throw new ArgumentOutOfRangeException(
                    nameof(deltaTime),
                    "Evaluation delta time must be finite and non-negative.");
            }
            _graph.Evaluate(deltaTime);
        }

        public void Dispose()
        {
            if (_disposed)
                return;

            _disposed = true;
            CleanupResources();
            _target = null;
            GC.SuppressFinalize(this);
        }

        private void CleanupResources()
        {
            try
            {
                if (_graph.IsValid())
                    _graph.Destroy();
            }
            finally
            {
                if (_muscles.IsCreated)
                    _muscles.Dispose();
                if (_muscleHandles.IsCreated)
                    _muscleHandles.Dispose();
                _posePlayable = default;
                _graph = default;
            }
        }

        private static void ValidateTarget(Animator target)
        {
            if (target == null)
                throw new ArgumentNullException(nameof(target));
            if (!target.gameObject.activeInHierarchy)
            {
                throw new ArgumentException(
                    "The Humanoid target must be active in the hierarchy.",
                    nameof(target));
            }
            if (target.avatar == null || !target.avatar.isValid || !target.avatar.isHuman)
            {
                throw new ArgumentException(
                    "The target requires a valid Humanoid Avatar.",
                    nameof(target));
            }
        }

        private static void ValidatePose(HumanPose pose)
        {
            if (pose.muscles == null || pose.muscles.Length != MuscleHandle.muscleHandleCount)
            {
                throw new ArgumentException(
                    $"HumanPose.muscles must contain {MuscleHandle.muscleHandleCount} values.",
                    nameof(pose));
            }
            if (!IsFinite(pose.bodyPosition) || !IsFinite(pose.bodyRotation))
            {
                throw new ArgumentException(
                    "HumanPose contains a non-finite body transform.",
                    nameof(pose));
            }
            if (Quaternion.Dot(pose.bodyRotation, pose.bodyRotation) < 0.000001f)
            {
                throw new ArgumentException(
                    "HumanPose body rotation has zero length.",
                    nameof(pose));
            }

            foreach (var muscle in pose.muscles)
            {
                if (!IsFinite(muscle))
                {
                    throw new ArgumentException(
                        "HumanPose contains a non-finite muscle value.",
                        nameof(pose));
                }
            }
        }

        private static void ValidateTrajectory(Vector3 position, Quaternion rotation)
        {
            if (!IsFinite(position) || !IsFinite(rotation))
                throw new ArgumentException("Trajectory contains a non-finite transform.");
            if (Quaternion.Dot(rotation, rotation) < 0.000001f)
                throw new ArgumentException("Trajectory rotation has zero length.");
        }

        private static MuscleHandle[] BuildHumanPoseOrderedMuscleHandles()
        {
            var humanPoseNames = HumanTrait.MuscleName;
            var unorderedHandles = new MuscleHandle[MuscleHandle.muscleHandleCount];
            MuscleHandle.GetMuscleHandles(unorderedHandles);
            if (humanPoseNames == null ||
                humanPoseNames.Length != HumanTrait.MuscleCount ||
                unorderedHandles.Length != humanPoseNames.Length)
            {
                throw new InvalidOperationException(
                    "Unity HumanTrait and MuscleHandle capabilities have different muscle counts.");
            }

            var handlesByCanonicalName = new Dictionary<string, MuscleHandle>(
                unorderedHandles.Length,
                StringComparer.Ordinal);
            foreach (var handle in unorderedHandles)
            {
                if (string.IsNullOrEmpty(handle.name) ||
                    !handlesByCanonicalName.TryAdd(handle.name, handle))
                {
                    throw new InvalidOperationException(
                        $"MuscleHandle name is empty or duplicated: '{handle.name}'.");
                }
            }

            var orderedHandles = new MuscleHandle[humanPoseNames.Length];
            var seenCanonicalNames = new HashSet<string>(StringComparer.Ordinal);
            for (var index = 0; index < humanPoseNames.Length; index++)
            {
                var name = humanPoseNames[index];
                var canonicalName = CanonicalHumanTraitMuscleName(index, name);
                if (string.IsNullOrEmpty(name) || !seenCanonicalNames.Add(canonicalName))
                {
                    throw new InvalidOperationException(
                        $"HumanTrait muscle canonical name is empty or duplicated: '{name}'.");
                }
                if (!handlesByCanonicalName.TryGetValue(canonicalName, out orderedHandles[index]))
                {
                    throw new InvalidOperationException(
                        $"HumanTrait muscle '{name}' has no matching MuscleHandle '{canonicalName}'.");
                }
            }

            if (seenCanonicalNames.Count != handlesByCanonicalName.Count)
                throw new InvalidOperationException("Muscle name mapping is not bijective.");
            return orderedHandles;
        }

        private static string CanonicalHumanTraitMuscleName(int muscleIndex, string name)
        {
            if (string.IsNullOrEmpty(name))
                return name;

            var bone = (HumanBodyBones)HumanTrait.BoneFromMuscle(muscleIndex);
            var firstFingerBone = (int)HumanBodyBones.LeftThumbProximal;
            var fingerBoneOffset = (int)bone - firstFingerBone;
            var fingerBoneCount =
                (int)HumanBodyBones.RightLittleDistal - firstFingerBone + 1;
            if (fingerBoneOffset < 0 || fingerBoneOffset >= fingerBoneCount)
                return name;

            var sideEnd = name.IndexOf(' ');
            var digitEnd = sideEnd < 0 ? -1 : name.IndexOf(' ', sideEnd + 1);
            if (sideEnd <= 0 || digitEnd <= sideEnd + 1 || digitEnd >= name.Length - 1)
            {
                throw new InvalidOperationException(
                    $"Finger muscle name has an unsupported format: '{name}'.");
            }

            var side = name.Substring(0, sideEnd);
            var digit = name.Substring(sideEnd + 1, digitEnd - sideEnd - 1);
            var suffix = name.Substring(digitEnd + 1);
            var boneName = bone.ToString();
            if (!boneName.StartsWith(side + digit, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    $"Finger muscle '{name}' does not match its HumanTrait bone '{boneName}'.");
            }
            return $"{side}Hand.{digit}.{suffix}";
        }

        private static Quaternion Normalize(Quaternion value)
        {
            var scale = 1f / Mathf.Sqrt(Quaternion.Dot(value, value));
            return new Quaternion(
                value.x * scale,
                value.y * scale,
                value.z * scale,
                value.w * scale);
        }

        private static bool IsFinite(Vector3 value)
        {
            return IsFinite(value.x) && IsFinite(value.y) && IsFinite(value.z);
        }

        private static bool IsFinite(Quaternion value)
        {
            return IsFinite(value.x) && IsFinite(value.y) &&
                   IsFinite(value.z) && IsFinite(value.w);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }

        private void EnsureValid()
        {
            if (_disposed)
                throw new ObjectDisposedException(nameof(HumanoidPosePlayable));
            if (_target == null)
                throw new InvalidOperationException("The Humanoid target was destroyed.");
            if (!_graph.IsValid())
                throw new InvalidOperationException("The Humanoid pose graph is invalid.");
        }

        private struct HumanPoseWriterJob : IAnimationJob
        {
            [ReadOnly] public NativeArray<MuscleHandle> MuscleHandles;
            [ReadOnly] public NativeArray<float> Muscles;
            public Vector3 BodyPosition;
            public Quaternion BodyRotation;
            public Vector3 TrajectoryPosition;
            public Quaternion TrajectoryRotation;

            public void ProcessAnimation(AnimationStream stream)
            {
                var human = stream.AsHuman();
                if (!human.isValid)
                    return;

                human.ResetToStancePose();
                for (var index = 0; index < MuscleHandles.Length; index++)
                    human.SetMuscle(MuscleHandles[index], Muscles[index]);
                human.bodyLocalPosition =
                    BodyPosition * human.humanScale + TrajectoryPosition;
                human.bodyLocalRotation = TrajectoryRotation * BodyRotation;
            }

            public void ProcessRootMotion(AnimationStream stream)
            {
            }
        }
    }
}
