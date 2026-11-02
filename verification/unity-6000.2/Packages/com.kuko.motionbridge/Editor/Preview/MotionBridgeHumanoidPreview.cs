using System;
using Kuko.MotionBridge.Retargeting;
using UnityEditor;
using UnityEngine;

namespace Kuko.MotionBridge.Editor.Preview
{
    /// <summary>
    /// Owns the single edit-mode PlayableGraph that writes a HumanPose to a Humanoid Animator.
    /// </summary>
    public sealed class MotionBridgeHumanoidPreview : IDisposable
    {
        private static MotionBridgeHumanoidPreview s_activePreview;

        private Animator _target;
        private AnimationModeDriver _animationModeDriver;
        private HumanoidPosePlayable _posePlayable;
        private TransformSnapshot[] _transformSnapshots;
        private AnimatorSnapshot _animatorSnapshot;
        private bool _isRunning;
        private bool _isDisposed;

        static MotionBridgeHumanoidPreview()
        {
            AssemblyReloadEvents.beforeAssemblyReload += StopActiveBeforeReload;
            EditorApplication.quitting += StopActiveBeforeReload;
            EditorApplication.playModeStateChanged += StopActiveBeforePlayMode;
        }

        public bool IsRunning => _isRunning;

        public Animator Target => _isRunning && _target != null ? _target : null;

        /// <summary>
        /// Starts an edit-mode preview and snapshots everything that the graph can modify.
        /// </summary>
        public void Start(Animator target)
        {
            ThrowIfDisposed();
            if (_isRunning)
                throw new InvalidOperationException("This Humanoid preview is already running.");
            ValidateTarget(target);
            if (s_activePreview != null && s_activePreview._isRunning)
                throw new InvalidOperationException("Another Motion Bridge Humanoid preview is active.");
            if (AnimationMode.InAnimationMode())
            {
                throw new InvalidOperationException(
                    "Unity Animation Mode is already owned by another preview or the Animation window.");
            }

            _target = target;
            _transformSnapshots = CaptureTransforms(target.transform);
            _animatorSnapshot = AnimatorSnapshot.Capture(target);

            try
            {
                _animationModeDriver =
                    ScriptableObject.CreateInstance<MotionBridgeAnimationModeDriver>();
                _animationModeDriver.hideFlags = HideFlags.HideAndDontSave;
                _posePlayable = new HumanoidPosePlayable(
                    target,
                    "Motion Bridge Humanoid Preview");

                if (target.cullingMode != AnimatorCullingMode.AlwaysAnimate)
                    target.cullingMode = AnimatorCullingMode.AlwaysAnimate;
                if (target.applyRootMotion)
                    target.applyRootMotion = false;
                if (target.runtimeAnimatorController != null)
                    target.runtimeAnimatorController = null;
                AnimationMode.StartAnimationMode(_animationModeDriver);

                _isRunning = true;
                s_activePreview = this;
            }
            catch
            {
                CleanupAndRestore();
                throw;
            }
        }

        /// <summary>
        /// Samples one retargetable HumanPose through AnimationHumanStream.
        /// </summary>
        public void Sample(HumanPose pose)
        {
            Sample(pose, Vector3.zero, Quaternion.identity);
        }

        /// <summary>
        /// Samples one pose and applies its trajectory once, relative to the target's start anchor.
        /// </summary>
        public void Sample(
            HumanPose pose,
            Vector3 trajectoryPosition,
            Quaternion trajectoryRotation)
        {
            ThrowIfDisposed();
            if (!_isRunning)
                throw new InvalidOperationException("The Humanoid preview is not running.");
            if (_target == null)
            {
                CleanupAndRestore();
                throw new InvalidOperationException("The preview target was destroyed.");
            }

            try
            {
                _posePlayable.SetPose(
                    pose,
                    trajectoryPosition,
                    trajectoryRotation);

                var samplingStarted = false;
                try
                {
                    AnimationMode.BeginSampling();
                    samplingStarted = true;
                    AnimationMode.SamplePlayableGraph(_posePlayable.Graph, 0, 0f);
                }
                finally
                {
                    if (samplingStarted)
                        AnimationMode.EndSampling();
                }

                EditorApplication.QueuePlayerLoopUpdate();
                SceneView.RepaintAll();
            }
            catch
            {
                CleanupAndRestore();
                throw;
            }
        }

        /// <summary>
        /// Stops sampling and restores the Animator, every child transform, and scene dirty state.
        /// </summary>
        public void Stop()
        {
            CleanupAndRestore();
        }

        public void Dispose()
        {
            if (_isDisposed)
                return;

            CleanupAndRestore();
            _isDisposed = true;
            GC.SuppressFinalize(this);
        }

        private static void StopActiveBeforeReload()
        {
            s_activePreview?.CleanupAndRestore();
        }

        private static void StopActiveBeforePlayMode(PlayModeStateChange state)
        {
            if (state == PlayModeStateChange.ExitingEditMode)
                StopActiveBeforeReload();
        }

        private void CleanupAndRestore()
        {
            var targetStillExists = _target != null;
            try
            {
                if (_animationModeDriver != null &&
                    AnimationMode.InAnimationMode(_animationModeDriver))
                {
                    AnimationMode.StopAnimationMode(_animationModeDriver);
                }
            }
            catch (Exception exception)
            {
                Debug.LogException(exception);
            }

            DisposeSafely(_posePlayable);
            _posePlayable = null;

            if (_animationModeDriver != null)
                UnityEngine.Object.DestroyImmediate(_animationModeDriver);
            _animationModeDriver = null;

            if (targetStillExists)
            {
                _animatorSnapshot.Restore(_target);
                RestoreTransforms(_transformSnapshots);
            }

            _transformSnapshots = null;
            _target = null;
            _isRunning = false;
            if (ReferenceEquals(s_activePreview, this))
                s_activePreview = null;
            SceneView.RepaintAll();
        }

        private static TransformSnapshot[] CaptureTransforms(Transform root)
        {
            var transforms = root.GetComponentsInChildren<Transform>(true);
            var snapshots = new TransformSnapshot[transforms.Length];
            for (var index = 0; index < transforms.Length; index++)
                snapshots[index] = TransformSnapshot.Capture(transforms[index]);
            return snapshots;
        }

        private static void RestoreTransforms(TransformSnapshot[] snapshots)
        {
            if (snapshots == null)
                return;

            foreach (var snapshot in snapshots)
                snapshot.Restore();
        }

        private static void ValidateTarget(Animator target)
        {
            if (target == null)
                throw new ArgumentNullException(nameof(target));
            if (EditorApplication.isPlayingOrWillChangePlaymode)
                throw new InvalidOperationException("Humanoid preview is available only in Edit Mode.");
            if (EditorUtility.IsPersistent(target))
                throw new ArgumentException("The preview target must be a scene instance.", nameof(target));
            if (!target.gameObject.activeInHierarchy)
                throw new ArgumentException("The preview target must be active in the hierarchy.", nameof(target));
            if (target.avatar == null || !target.avatar.isValid || !target.avatar.isHuman)
                throw new ArgumentException("The preview target requires a valid Humanoid Avatar.", nameof(target));
            if (!HumanoidTargetScale.IsPositiveUniformHierarchy(target.transform))
            {
                throw new ArgumentException(
                    "The preview target and its parents require positive uniform scale.",
                    nameof(target));
            }
        }

        private void ThrowIfDisposed()
        {
            if (_isDisposed)
                throw new ObjectDisposedException(nameof(MotionBridgeHumanoidPreview));
        }

        private static void DisposeSafely(IDisposable disposable)
        {
            if (disposable == null)
                return;
            try
            {
                disposable.Dispose();
            }
            catch (Exception exception)
            {
                Debug.LogException(exception);
            }
        }

        private sealed class MotionBridgeAnimationModeDriver : AnimationModeDriver
        {
        }

        private readonly struct TransformSnapshot
        {
            private readonly Transform _transform;
            private readonly Vector3 _localPosition;
            private readonly Quaternion _localRotation;
            private readonly Vector3 _localScale;

            private TransformSnapshot(Transform transform)
            {
                _transform = transform;
                _localPosition = transform.localPosition;
                _localRotation = transform.localRotation;
                _localScale = transform.localScale;
            }

            public static TransformSnapshot Capture(Transform transform)
            {
                return new TransformSnapshot(transform);
            }

            public void Restore()
            {
                if (_transform == null)
                    return;

                if (_transform.localPosition != _localPosition)
                    _transform.localPosition = _localPosition;
                if (_transform.localRotation != _localRotation)
                    _transform.localRotation = _localRotation;
                if (_transform.localScale != _localScale)
                    _transform.localScale = _localScale;
            }
        }

        private readonly struct AnimatorSnapshot
        {
            private readonly bool _enabled;
            private readonly bool _applyRootMotion;
            private readonly AnimatorUpdateMode _updateMode;
            private readonly AnimatorCullingMode _cullingMode;
            private readonly float _speed;
            private readonly RuntimeAnimatorController _controller;
            private readonly Avatar _avatar;
            private readonly bool _fireEvents;
            private readonly bool _stabilizeFeet;
            private readonly float _feetPivotActive;
            private readonly bool _layersAffectMassCenter;
            private readonly bool _logWarnings;
            private readonly bool _keepControllerState;
            private readonly bool _writeDefaultsOnDisable;

            private AnimatorSnapshot(Animator animator)
            {
                _enabled = animator.enabled;
                _applyRootMotion = animator.applyRootMotion;
                _updateMode = animator.updateMode;
                _cullingMode = animator.cullingMode;
                _speed = animator.speed;
                _controller = animator.runtimeAnimatorController;
                _avatar = animator.avatar;
                _fireEvents = animator.fireEvents;
                _stabilizeFeet = animator.stabilizeFeet;
                _feetPivotActive = animator.feetPivotActive;
                _layersAffectMassCenter = animator.layersAffectMassCenter;
                _logWarnings = animator.logWarnings;
                _keepControllerState = animator.keepAnimatorStateOnDisable;
                _writeDefaultsOnDisable = animator.writeDefaultValuesOnDisable;
            }

            public static AnimatorSnapshot Capture(Animator animator)
            {
                return new AnimatorSnapshot(animator);
            }

            public void Restore(Animator animator)
            {
                if (animator == null)
                    return;

                if (animator.avatar != _avatar)
                    animator.avatar = _avatar;
                if (animator.runtimeAnimatorController != _controller)
                    animator.runtimeAnimatorController = _controller;
                if (animator.applyRootMotion != _applyRootMotion)
                    animator.applyRootMotion = _applyRootMotion;
                if (animator.updateMode != _updateMode)
                    animator.updateMode = _updateMode;
                if (animator.cullingMode != _cullingMode)
                    animator.cullingMode = _cullingMode;
                if (animator.speed != _speed)
                    animator.speed = _speed;
                if (animator.fireEvents != _fireEvents)
                    animator.fireEvents = _fireEvents;
                if (animator.stabilizeFeet != _stabilizeFeet)
                    animator.stabilizeFeet = _stabilizeFeet;
                if (animator.feetPivotActive != _feetPivotActive)
                    animator.feetPivotActive = _feetPivotActive;
                if (animator.layersAffectMassCenter != _layersAffectMassCenter)
                    animator.layersAffectMassCenter = _layersAffectMassCenter;
                if (animator.logWarnings != _logWarnings)
                    animator.logWarnings = _logWarnings;
                if (animator.keepAnimatorStateOnDisable != _keepControllerState)
                    animator.keepAnimatorStateOnDisable = _keepControllerState;
                if (animator.writeDefaultValuesOnDisable != _writeDefaultsOnDisable)
                    animator.writeDefaultValuesOnDisable = _writeDefaultsOnDisable;
                if (animator.enabled != _enabled)
                    animator.enabled = _enabled;
            }
        }
    }
}
