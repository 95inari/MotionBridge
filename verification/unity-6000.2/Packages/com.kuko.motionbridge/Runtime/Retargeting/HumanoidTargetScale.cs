using UnityEngine;

namespace Kuko.MotionBridge.Retargeting
{
    /// <summary>
    /// Shared scale gate for the setup guide and both Edit/Play preview writers.
    /// </summary>
    public static class HumanoidTargetScale
    {
        public static bool IsPositiveUniformHierarchy(Transform target)
        {
            if (target == null)
                return false;
            for (var current = target; current != null; current = current.parent)
            {
                var scale = current.localScale;
                var maximum = Mathf.Max(
                    Mathf.Abs(scale.x),
                    Mathf.Abs(scale.y),
                    Mathf.Abs(scale.z));
                var tolerance = Mathf.Max(0.00001f, maximum * 0.0001f);
                if (scale.x <= 0f || scale.y <= 0f || scale.z <= 0f ||
                    Mathf.Abs(scale.x - scale.y) > tolerance ||
                    Mathf.Abs(scale.x - scale.z) > tolerance)
                {
                    return false;
                }
            }
            return true;
        }
    }
}
