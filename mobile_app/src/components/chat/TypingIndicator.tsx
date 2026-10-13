import { useEffect, useRef } from 'react';
import { Animated, Easing, Text, View } from 'react-native';

const DOTS = [0, 1, 2];
const STEP_MS = 160;

/** "ИИ печатает…": three bouncing dots inside an assistant-styled bubble. */
export function TypingIndicator() {
  const values = useRef(DOTS.map(() => new Animated.Value(0))).current;

  useEffect(() => {
    const loops = values.map((value, i) =>
      Animated.loop(
        Animated.sequence([
          Animated.delay(i * STEP_MS),
          Animated.timing(value, { toValue: 1, duration: 320, easing: Easing.out(Easing.quad), useNativeDriver: true }),
          Animated.timing(value, { toValue: 0, duration: 320, easing: Easing.in(Easing.quad), useNativeDriver: true }),
          Animated.delay((DOTS.length - 1 - i) * STEP_MS),
        ]),
      ),
    );
    loops.forEach((loop) => loop.start());
    return () => loops.forEach((loop) => loop.stop());
  }, [values]);

  return (
    <View className="mb-2 w-full flex-row justify-start" accessibilityLiveRegion="polite" accessibilityLabel="ИИ печатает">
      <View className="flex-row items-center rounded-2xl rounded-bl-md border border-slate-700/60 bg-slate-800 px-4 py-3.5">
        {values.map((value, i) => (
          <Animated.View
            key={i}
            className="mr-1.5 h-2 w-2 rounded-full bg-slate-400"
            style={{
              opacity: value.interpolate({ inputRange: [0, 1], outputRange: [0.35, 1] }),
              transform: [{ translateY: value.interpolate({ inputRange: [0, 1], outputRange: [0, -4] }) }],
            }}
          />
        ))}
        <Text className="ml-1.5 text-sm text-slate-400">ИИ печатает…</Text>
      </View>
    </View>
  );
}
