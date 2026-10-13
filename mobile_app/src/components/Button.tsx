import { ActivityIndicator, Pressable, Text } from 'react-native';

type Props = {
  title: string;
  onPress: () => void;
  loading?: boolean;
  disabled?: boolean;
  variant?: 'primary' | 'secondary';
};

export function Button({ title, onPress, loading = false, disabled = false, variant = 'primary' }: Props) {
  const inactive = disabled || loading;
  const primary = variant === 'primary';
  return (
    <Pressable
      onPress={onPress}
      disabled={inactive}
      accessibilityRole="button"
      accessibilityState={{ disabled: inactive, busy: loading }}
      className={`h-14 items-center justify-center rounded-xl active:opacity-80 ${
        primary ? 'bg-indigo-500' : 'border border-neutral-700 bg-neutral-900'
      } ${inactive ? 'opacity-60' : ''}`}>
      {loading ? <ActivityIndicator color="#ffffff" /> : <Text className="text-base font-semibold text-white">{title}</Text>}
    </Pressable>
  );
}
